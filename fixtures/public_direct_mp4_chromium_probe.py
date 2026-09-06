#!/usr/bin/env python3
"""Real Chromium proof: direct-MP4 navigation (built-in player document).

Navigates the address bar straight to the public progressive MP4 on the
already-proven Plyr CDN host — no player page, no custom controls, no
injected element. The generic media button must still attach to the
built-in video document, capture the current MP4, and finalize
byte/hash-identical output with no browser download.
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
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_dash_quality_probe as adaptive
import hls_fmp4_probe as hls
import public_chromium_probe as public
import public_plyr_chromium_probe as plyr
import segmented_restart_probe as support

PAGE = "https://cdn.plyr.io/static/demo/View_From_A_Blue_Moon_Trailer-576p.mp4"
BIN = public.BIN
EXTENSION = public.EXTENSION


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_direct_player(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=Array.from(document.querySelectorAll('video'));"
                "const v=media[0];"
                "if(v){v.muted=true;void v.play().catch(()=>{});}"
                "const r=v?.getBoundingClientRect();"
                "return {href:location.href,videos:media.length,"
                "src:v?(v.currentSrc||v.src||''):'',"
                "readyState:v?.readyState||0,paused:v?.paused??true,"
                "duration:v?.duration||0,"
                "rect:r?{w:r.width,h:r.height}:null,"
                "button:!!document.querySelector('#dm-media-download-button')};})())"
            )
            last = json.loads(raw)
            if last.get("videos", 0) >= 1 and last.get("readyState", 0) >= 2 and last.get("button"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"direct MP4 document did not become playable/injected: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-direct-mp4-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "Managed" / "blue-moon-direct.mp4"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
             f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
             f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
             "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        client = adaptive.connect_event_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        player = wait_direct_player(client)
        print("DIRECT-MP4-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        point = plyr.button_point(client)
        print("DIRECT-MP4-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = plyr.wait_media_job(db)
        print("DIRECT-MP4-NATIVE-JOB:", json.dumps({"id": job["id"], "source": plyr.redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "name": job.get("name")}, sort_keys=True), flush=True)
        if urlsplit(job.get("source", "")).netloc.lower() != "cdn.plyr.io":
            raise RuntimeError(f"native job selected wrong source: {job}")
        public.commit_via_cli(str(home), inspector_port, job["id"], "blue-moon-direct.mp4", str(managed))
        done = public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        reference = plyr.browser_reference(client, job["source"])
        print("BROWSER-DIRECT-MP4-REFERENCE:", json.dumps({"url": plyr.redacted_url(job["source"]), **reference}, sort_keys=True), flush=True)
        if native_size != int(reference["size"]) or native_hash != reference["hash"]:
            raise RuntimeError(f"native output differs from browser source: native={native_size}/{native_hash} browser={reference}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(plyr.jobs(db)) == 1, plyr.jobs(db)
        assert not browser_files, browser_files
        print(f"DIRECT-MP4: PASS (page={PAGE}, source={plyr.redacted_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("DIRECT-MP4-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
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
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_DIRECT_MP4") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"DIRECT-MP4-PROBE: FAIL: {error}", flush=True)
        raise
