#!/usr/bin/env python3
"""Real public host: Odysee watch page -> DM button -> progressive MP4 capture.

Odysee (a production LBRY-based video host, new family for this loop) serves
a plain progressive MP4 (`player.odycdn.com/v6/streams/.../*.mp4`) to a
single <video> element. The probe discovers a real watch page at runtime
from Discover (skipping state-media channels), starts playback with a
trusted play click, clicks the REAL extension media button, and commits
through the real provisional-to-completed flow. Reference = an independent
fetch (Chrome UA) of the exact source URL the job captured. Asserts
byte/hash equality, exactly one job, empty Chromium Downloads.
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
import public_chromium_probe as public
import segmented_restart_probe as support
import hls_fmp4_probe as hls

BIN = public.BIN
EXTENSION = public.EXTENSION
DISCOVER = "https://odysee.com/$/discover?t=all"
CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/149.0.0.0 Safari/537.36"
# State outlets are legal to fetch but out of place as our test media.
SKIP_CHANNELS = ("rtde", "rtdocumentary", "rtnews", "sputnik")

LINKS_JS = (
    "JSON.stringify((()=>[...document.querySelectorAll('a[href*=\"/@\"]')]"
    ".map(a=>a.href).filter(h=>/@[^/]+\\/[^/$]/.test(h)).slice(0,20))())"
)
PLAY_JS = (
    "JSON.stringify((()=>{const b=document.querySelector('video');"
    "const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
)
BUTTON_JS = (
    "JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');"
    "if(!b)return null;const r=b.getBoundingClientRect();"
    "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
)
STATE_JS = (
    "JSON.stringify((()=>{const v=document.querySelector('video');"
    "return {t:v?v.currentTime:-1,src:((v&&(v.currentSrc||v.src))||'').slice(0,140)};})())"
)


def click(client, point) -> None:
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})


def wait_media_job(db, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: list = []
    while time.time() < deadline:
        try:
            jobs = public.jobs(db)
            media = [job for job in jobs if job.get("media")]
            if media:
                return media[-1]
            last = [job.get("id") for job in jobs]
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"no media job appeared; jobs seen: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-odysee-button-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    managed_dir.mkdir(parents=True)
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
        client.call("Page.navigate", {"url": DISCOVER})
        time.sleep(10.0)
        links = [url for url in json.loads(client.evaluate(LINKS_JS))
                 if not any(skip in url.lower() for skip in SKIP_CHANNELS)]
        if not links:
            raise RuntimeError("no usable watch page on Discover")
        page = links[0]
        print(f"ODYSEE-PAGE: {page}", flush=True)
        client.call("Page.navigate", {"url": page})
        time.sleep(10.0)
        try:
            prestate = json.loads(client.evaluate(STATE_JS))
        except Exception:
            prestate = {"t": 0}
        if not prestate or prestate.get("t", 0) <= 0:
            click(client, json.loads(client.evaluate(PLAY_JS)))
            print("ODYSEE-PLAY-CLICK: issued", flush=True)
        else:
            print(f"ODYSEE-PLAY-CLICK: skipped (already t={prestate.get('t')})", flush=True)
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
        print("ODYSEE-STATE:", json.dumps(state, sort_keys=True), flush=True)
        if point is None:
            raise RuntimeError(f"media button never appeared: {state}")
        click(client, point)
        db = public.db_path(str(home))
        created = wait_media_job(db)
        source = created.get("source")
        print("ODYSEE-JOB:", json.dumps({"id": created["id"], "media": created.get("media"), "kind": created.get("kind"), "source": (source or "")[:140]}, sort_keys=True), flush=True)
        assert created.get("media") is True and source and not source.startswith("blob:"), created
        print("ODYSEE-REFERENCE-FETCH: independent fetch of captured source...", flush=True)
        source_request = urllib.request.Request(source, headers={"User-Agent": CHROME_UA, "Referer": page})
        with urllib.request.urlopen(source_request, timeout=300) as source_response:
            reference = source_response.read()
        reference_hash = hashlib.sha256(reference).hexdigest()
        print(f"ODYSEE-REFERENCE: bytes={len(reference)} sha256={reference_hash}", flush=True)
        managed = managed_dir / "odysee-capture.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "odysee-capture.mp4", str(managed))
        done = public.wait_completed(db, created["id"], timeout=600)
        assert done.get("provisional") is False, done
        output = managed.read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert len(output) == len(reference) and output_hash == reference_hash, (len(output), output_hash)
        all_jobs = public.jobs(db)
        assert len(all_jobs) == 1, [job.get("id") for job in all_jobs]
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"ODYSEE: PASS (output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("ODYSEE-PROBE: PASS", flush=True)
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
        print(f"ODYSEE-PROBE: FAIL: {error}", flush=True)
        raise
