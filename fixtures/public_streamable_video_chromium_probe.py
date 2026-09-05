#!/usr/bin/env python3
"""Real Chromium/native proof: generic media capture on a production video page.

Streamable's real public watch page for video o6xqa3 ships the media in the
top document: a native <video> whose src is the server-issued signed CDN mp4
URL (cdn-cf-east.streamable.com/video/mp4/o6xqa3.mp4?...Key-Pair-Id...
Signature=...). A trusted click on the player's real play control
(div.svp-button-play, aria-label "Play (k)") starts playback; the extension's
generic media button must appear over the playing video, and the resident must
capture the exact mp4 (byte/hash-identical to an independent fetch of the same
signed source).
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
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

PAGE = "https://streamable.com/o6xqa3"
EXPECTED_SOURCE_HOST = "cdn-cf-east.streamable.com"
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME
UA = "Download Manager/0.1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_json(client, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def click(client, point: dict) -> None:
    for event, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call("Input.dispatchMouseEvent", {"type": event, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1, "modifiers": 0})


def wait_page(client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = evaluate_json(
                client,
                "{href:location.href,state:document.readyState,videos:[...document.querySelectorAll('video')].map(v=>v.currentSrc||v.src||''),play:!!document.querySelector('div.svp-button-play[aria-label=\\\"Play (k)\\\"]')}",
            )
            if (
                last.get("state") == "complete"
                and last.get("href", "").startswith("https://streamable.com/")
                and any(EXPECTED_SOURCE_HOST in src for src in last.get("videos", []))
                and last.get("play")
            ):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Streamable page did not become ready: {last}")


def play_control(client) -> dict:
    result = evaluate_json(
        client,
        "(()=>{const b=document.querySelector('div.svp-button-play[aria-label=\\\"Play (k)\\\"]');if(!b)return {error:'play control missing'};"
        "b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();"
        "return {aria:b.getAttribute('aria-label'),cls:b.className,x:r.left+r.width/2,y:r.top+r.height/2};})()",
    )
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def wait_playing(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = evaluate_json(
            client,
            "(()=>{const media=[...document.querySelectorAll('video,audio')].find(el=>{const src=el.currentSrc||el.src||'';return /^https?:/.test(src)&&!el.paused&&el.readyState>=2&&!el.ended;});"
            "if(!media)return {found:false};const src=media.currentSrc||media.src||'';"
            "const r=media.getBoundingClientRect();const b=document.querySelector('#dm-media-download-button');const br=b?.getBoundingClientRect();"
            "return {found:true,tag:media.tagName.toLowerCase(),src,readyState:media.readyState,paused:media.paused,currentTime:media.currentTime,duration:media.duration,"
            "rect:r?{w:r.width,h:r.height,top:r.top,left:r.left}:null,button:!!b,buttonRect:br?{x:br.left+br.width/2,y:br.top+br.height/2}:null};})()",
        )
        if last.get("found") and last.get("button") and last.get("buttonRect"):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"Streamable player did not become playable/injected: {last}")


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return response.read()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-streamable-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
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
            [str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]
            + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", PAGE],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        page = wait_page(client)
        print("STREAMABLE-PAGE:", json.dumps(page, sort_keys=True), flush=True)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        control = play_control(client)
        print("STREAMABLE-PLAY-CONTROL:", json.dumps(control, sort_keys=True), flush=True)
        click(client, control)
        playing = wait_playing(client)
        print("STREAMABLE-PLAYING:", json.dumps(playing, sort_keys=True), flush=True)
        button = playing["buttonRect"]
        click(client, button)
        db = public.db_path(str(home))
        deadline = time.time() + 90
        job = None
        while time.time() < deadline:
            candidates = [item for item in public.jobs(db) if item.get("media") is True and EXPECTED_SOURCE_HOST in item.get("source", "")]
            if candidates:
                job = candidates[-1]
                break
            time.sleep(0.5)
        if not job:
            raise RuntimeError(f"no native media job for Streamable video: {public.jobs(db)}")
        print("STREAMABLE-NATIVE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "media": job.get("media"), "state": job.get("state"), "name": job.get("name")}, sort_keys=True), flush=True)
        media_name = "streamable-o6xqa3.mp4"
        managed = managed_dir / media_name
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        reference_dir = root / "reference"
        reference_dir.mkdir(parents=True, exist_ok=True)
        reference_path = reference_dir / "source.mp4"
        reference_path.write_bytes(fetch_bytes(job["source"]))
        reference = {"size": reference_path.stat().st_size, "sha256": sha256(reference_path)}
        print("STREAMABLE-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        completed = public.wait_completed(db, job["id"], timeout=300)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        assert native_size == reference["size"], (completed, native_size, reference)
        assert native_hash == reference["sha256"], (completed, native_hash, reference)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"STREAMABLE-VIDEO: PASS (page={PAGE}, source={job['source'][:90]}…, output_bytes={native_size}, sha256={native_hash}, jobs={len(public.jobs(db))}, browser_downloads={browser_files})", flush=True)
        print("STREAMABLE-VIDEO-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
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
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_STREAMABLE") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STREAMABLE-VIDEO-PROBE: FAIL: {error}", flush=True)
        raise