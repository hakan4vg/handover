#!/usr/bin/env python3
"""Real public player: Plyr custom controls -> DM button -> progressive MP4.

plyr.io embeds its CDN demo reel in a Plyr player with custom controls (no
native controls element). The probe starts playback with a trusted play
click, clicks the REAL extension media button, and commits through the real
provisional-to-completed flow. Reference = the CDN bytes fetched live.
Asserts byte/hash equality, exactly one job, empty Chromium Downloads.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://plyr.io/"
SOURCE = "https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4"

PLAY_JS = (
    "JSON.stringify((()=>{const b=document.querySelector('.plyr__control--overlaid')||document.querySelector('video');"
    "const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
)
BUTTON_JS = (
    "JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');"
    "if(!b)return null;const r=b.getBoundingClientRect();"
    "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
)
STATE_JS = (
    "JSON.stringify((()=>{const v=document.querySelector('video');"
    "return {t:v?v.currentTime:-1,src:((v&&(v.currentSrc||v.src))||'').slice(0,120)};})())"
)


def click(client, point) -> None:
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-plyr-button-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    managed_dir.mkdir(parents=True)
    print("PLYR-REFERENCE-FETCH: downloading CDN bytes...", flush=True)
    # cdn.plyr.io 403s Python's bare default UA; identify as the same
    # HeadlessChrome this probe drives (diagnosed 2026-09-06).
    request = urllib.request.Request(SOURCE, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/149.0.0.0 Safari/537.36"})
    with urllib.request.urlopen(request, timeout=120) as response:
        reference = response.read()
    reference_hash = hashlib.sha256(reference).hexdigest()
    print(f"PLYR-REFERENCE: bytes={len(reference)} sha256={reference_hash}", flush=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    app_log.close()
    chrome = None
    client = None
    try:
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
        client.call("Page.navigate", {"url": PAGE})
        time.sleep(8.0)
        click(client, json.loads(client.evaluate(PLAY_JS)))
        deadline = time.time() + 90
        point = None
        state = {}
        while time.time() < deadline:
            try:
                raw = client.evaluate(BUTTON_JS)
                state = json.loads(client.evaluate(STATE_JS))
                if raw and json.loads(raw):
                    point = json.loads(raw)
                    break
            except Exception as error:
                state = {"error": str(error)[:120]}
            time.sleep(1.0)
        print("PLYR-STATE:", json.dumps(state, sort_keys=True), flush=True)
        if point is None:
            raise RuntimeError(f"media button never appeared: {state}")
        click(client, point)
        db = public.db_path(str(home))
        created = public.wait_job(db, SOURCE, timeout=120)
        print("PLYR-JOB:", json.dumps({"id": created["id"], "media": created.get("media"), "kind": created.get("kind"), "source": created.get("source")}, sort_keys=True), flush=True)
        assert created.get("source") == SOURCE, created
        managed = managed_dir / "plyr-demo.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "plyr-demo.mp4", str(managed))
        done = public.wait_completed(db, created["id"], timeout=600)
        assert done.get("provisional") is False, done
        output = managed.read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert len(output) == len(reference) and output_hash == reference_hash, (len(output), output_hash)
        all_jobs = public.jobs(db)
        assert len(all_jobs) == 1, [job.get("id") for job in all_jobs]
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"PLYR: PASS (output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("PLYR-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}", flush=True)
        raise
    finally:
        if client is not None:
            try: client.sock.close()
            except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill(); chrome.wait(timeout=10)
        if app.poll() is None:
            app.terminate()
            try: app.wait(timeout=10)
            except subprocess.TimeoutExpired: app.kill(); app.wait(timeout=10)
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PLYR-PROBE: FAIL: {error}", flush=True)
        raise
