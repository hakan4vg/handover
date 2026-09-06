#!/usr/bin/env python3
"""Full-chain IDM loop on a real page: extension media button -> gated media.

A real hls.js player (CDN) on the fixture page plays the hotlink-gated fMP4
VOD (same-host playback carries a same-host Referer, so the page plays). The
probe drives the REAL extension media button with a trusted CDP click in a
fresh disposable profile, lets pageUrl flow through the REAL
native-messaging path, and commits through the real provisional-to-completed
flow. It asserts the job row carries the page as referrer, output is
byte/hash-identical to the ordered segment concatenation, exactly one job
exists, and Chromium downloaded nothing itself.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
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
MANIFEST = "/hls/gated-fmp4.m3u8"
FRAGMENTS = ["ginit.mp4", "g0.m4s", "g1.m4s", "g2.m4s"]

READINESS_JS = (
    "JSON.stringify((()=>{const v=document.querySelector('video');"
    "const b=document.querySelector('#dm-media-download-button');"
    "return {playing:!!v&&v.currentTime>0.5&&!v.paused,"
    "button:!!b,t:v?v.currentTime:-1,ready:v?v.readyState:-1};})())"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(client, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate(READINESS_JS))
            if last.get("playing") and last.get("button"):
                return last
        except Exception as error:
            last = {"error": str(error)[:120]}
        time.sleep(1.0)
    raise RuntimeError(f"player/button never ready: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-fullchain-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    managed_dir.mkdir(parents=True)
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "fixtures/server.py", "--port", str(port)],
        cwd="/srv/repos/downloadmanager",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    page_url = f"http://127.0.0.1:{port}/page/gated-hls.html"
    manifest_url = f"http://127.0.0.1:{port}{MANIFEST}"
    support.wait_http(page_url)
    fragments = {}
    for name in FRAGMENTS:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/hls/{name}", headers={"Referer": page_url}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            fragments[name] = response.read()
    track = str(root / "fullchain-reference-video.track")
    with open(track, "wb") as handle:
        for name in FRAGMENTS:
            handle.write(fragments[name])
    reference_path = str(root / "fullchain-reference.mp4")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", track, "-map", "0:0", "-c", "copy", reference_path],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
    reference_size = Path(reference_path).stat().st_size
    reference_hash = hashlib.sha256(Path(reference_path).read_bytes()).hexdigest()
    print(f"FULLCHAIN-REFERENCE: fragments=4 bytes={reference_size} sha256={reference_hash}", flush=True)
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
        client.call("Page.navigate", {"url": page_url})
        ready = wait_ready(client)
        print("FULLCHAIN-PLAYER:", json.dumps(ready, sort_keys=True), flush=True)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const r=document.querySelector('#dm-media-download-button').getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
        ))
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        created = public.wait_job(db, manifest_url, timeout=90)
        print("FULLCHAIN-JOB:", json.dumps({"id": created["id"], "media": created.get("media"), "source": created.get("source"), "referrer": created.get("referrer")}, sort_keys=True), flush=True)
        assert created.get("media") is True, created
        assert created.get("source") == manifest_url, created
        # The chain assertion: the capture page flowed through native
        # messaging onto the job row, so the gated segments replay it.
        assert created.get("referrer") == page_url, created
        managed = managed_dir / "fullchain-gated.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "fullchain-gated.mp4", str(managed))
        done = public.wait_completed(db, created["id"], timeout=300)
        assert done.get("provisional") is False, done
        output = managed.read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert len(output) == reference_size and output_hash == reference_hash, (len(output), output_hash)
        all_jobs = public.jobs(db)
        assert len(all_jobs) == 1, [job.get("id") for job in all_jobs]
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"FULLCHAIN: PASS (output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("FULLCHAIN-PROBE: PASS", flush=True)
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
        if server.poll() is None:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FULLCHAIN-PROBE: FAIL: {error}", flush=True)
        raise
