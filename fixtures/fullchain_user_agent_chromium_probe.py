#!/usr/bin/env python3
"""Red/green proof for replaying the browser User-Agent.

The local encrypted HLS page requires the same-host Referer and a browser
User-Agent. RED expects the current resident (which sends its own fixed UA) to
fail after the real extension button capture. GREEN expects the captured UA to
reach the resident and produce the exact independent reference.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gated_encrypted_hls_referrer_probe as encrypted
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION

READINESS_JS = (
    "JSON.stringify((()=>{const v=document.querySelector('video');"
    "const b=document.querySelector('#dm-media-download-button');"
    "return {playing:!!v&&v.currentTime>0.5&&!v.paused,button:!!b,"
    "t:v?v.currentTime:-1,ready:v?v.readyState:-1};})())"
)


def wait_ready(client, timeout: float = 150.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate(READINESS_JS))
            if last.get("playing") and last.get("button"):
                return last
        except Exception as error:
            last = {"error": str(error)[:160]}
        time.sleep(1.0)
    raise RuntimeError(f"player/button never ready: {last}")


def wait_terminal(db: str, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in public.jobs(db) if job.get("id") == job_id), None)
        if last is not None and last.get("state") in {"completed", "failed"}:
            return last
        time.sleep(0.2)
    raise RuntimeError(f"job did not reach a terminal state: {last}")


def click(client, point: dict) -> None:
    for event in ("mousePressed", "mouseReleased"):
        client.call("Input.dispatchMouseEvent", {
            "type": event, "x": point["x"], "y": point["y"],
            "button": "left", "clickCount": 1, "modifiers": 0,
        })


def main() -> int:
    expect_failure = os.environ.get("DM_EXPECT_UA_FAILURE") == "1"
    root = Path(tempfile.mkdtemp(prefix="dm-user-agent-context-"))
    home = root / "home"
    profile = root / "profile"
    managed_dir = root / "Managed"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    managed_dir.mkdir(parents=True)
    server = encrypted.GatedEncryptedHls(require_browser_user_agent=True)
    xvfb = app = chrome = client = None
    app_log_path = root / "app.log"
    try:
        reference, reference_hash = encrypted.fetch_reference(server, root)
        reference_size = reference.stat().st_size
        print(f"UA-REFERENCE: bytes={reference_size} sha256={reference_hash}", flush=True)
        public.write_native_manifest(str(home), str(profile))
        inspector_port = support.free_port()
        chrome_port = support.free_port()
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        with app_log_path.open("wb") as app_log:
            app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
        support.wait_db(str(home))
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
             f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
             f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
             "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": server.page_url})
        ready = wait_ready(client)
        browser_ua = client.evaluate("navigator.userAgent")
        print("UA-PLAYER:", json.dumps({**ready, "userAgent": browser_ua}, sort_keys=True), flush=True)
        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const r=document.querySelector('#dm-media-download-button').getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
        ))
        click(client, point)
        db = public.db_path(str(home))
        created = public.wait_job(db, server.source, timeout=120)
        print("UA-JOB:", json.dumps({
            "id": created["id"], "media": created.get("media"),
            "source": created.get("source"), "referrer": created.get("referrer"),
            "user_agent": created.get("userAgent"),
        }, sort_keys=True), flush=True)
        if expect_failure:
            failed = wait_terminal(db, created["id"])
            assert failed.get("state") == "failed" and "403" in str(failed.get("error")), failed
            print("UA-RED: PASS (resident UA was rejected without replayed browser User-Agent)", flush=True)
            print("UA-PROBE: RED PASS", flush=True)
            return 0

        assert created.get("media") is True and created.get("source") == server.source, created
        assert created.get("referrer") == server.page_url, created
        assert created.get("userAgent") == browser_ua, created
        managed = managed_dir / "user-agent-capture.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], managed.name, str(managed))
        done = public.wait_completed(db, created["id"], timeout=300)
        output = managed.read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert done.get("provisional") is False, done
        assert len(output) == reference_size and output_hash == reference_hash, (len(output), output_hash)
        assert len(public.jobs(db)) == 1
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"UA-GREEN: PASS (output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("UA-PROBE: GREEN PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')[-3000:]}", flush=True)
        raise
    finally:
        if client is not None:
            try:
                client.sock.close()
            except Exception:
                pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=10)
        if app is not None:
            support.terminate_only(app, "UA probe app")
        if server is not None:
            server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "UA probe Xvfb")
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"UA-PROBE: FAIL: {error}", flush=True)
        raise
