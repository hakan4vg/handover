#!/usr/bin/env python3
"""Real Chromium + Able Player accessible progressive media proof.

Able Player's official caption/description/chapters demo uses a custom
accessible control surface around a direct progressive source. This probe clicks the
real Able Player Play control with trusted Chromium input, captures one native
media job, commits it through the resident process, and compares the result to
an independent fetch of the exact media source.
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
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_dash_quality_probe as adaptive
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://ableplayer.github.io/ableplayer/demos/video3.html"
TARGET = "#video1"
SOURCE_HOST = "ableplayer.github.io"
SOURCE_PATH = "/ableplayer/media/wwa.mp4"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redacted_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def click(client: adaptive.EventCDP, point: dict) -> None:
    for event_type in ("mousePressed", "mouseReleased"):
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": point["x"],
                "y": point["y"],
                "button": "left",
                "clickCount": 1,
                "modifiers": 0,
            },
        )


def player_state(client: adaptive.EventCDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        f"const v=document.querySelector({json.dumps(TARGET)});"
        "const r=v?.getBoundingClientRect();"
        "const play=document.querySelector('.able-big-play-button,[aria-label=\\\"Play\\\"]');const pr=play?.getBoundingClientRect();"
        "const button=document.querySelector('#dm-media-download-button');const br=button?.getBoundingClientRect();"
        "return {target:!!v,source:v?(v.currentSrc||v.src||''):'',readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,videoRect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null,playPoint:pr?{x:pr.left+pr.width/2,y:pr.top+pr.height/2,width:pr.width,height:pr.height}:null,button:!!button,buttonPoint:br?{x:br.left+br.width/2,y:br.top+br.height/2,width:br.width,height:br.height}:null,buttonPointerEvents:button?getComputedStyle(button).pointerEvents:null,tracks:v?[...v.querySelectorAll('track')].length:0};})())"
    )
    return json.loads(raw)


def wait_ready(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            client.evaluate(
                f"(()=>{{const v=document.querySelector({json.dumps(TARGET)});v?.scrollIntoView({{block:'center',inline:'center'}});return v?.currentSrc||v?.src||'';}})()"
            )
            last = player_state(client)
            source = urlsplit(last.get("source", ""))
            if (
                last.get("target")
                and source.netloc.lower() == SOURCE_HOST
                and source.path == SOURCE_PATH
                and last.get("readyState", 0) >= 2
                and last.get("playPoint")
            ):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Able Player media did not become ready: {last}")


def wait_playing(client: adaptive.EventCDP, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = player_state(client)
            button = last.get("buttonPoint") or {}
            if (
                last.get("readyState", 0) >= 2
                and not last.get("paused")
                and last.get("button")
                and last.get("buttonPointerEvents") == "auto"
                and button.get("y", -1) > 0
                and button.get("y", -1) < 1000
            ):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Able Player media did not become playing/injected: {last}")


def independent_reference(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/149 Safari/537.36",
            "Referer": PAGE,
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        if response.status != 200:
            raise RuntimeError(f"independent reference HTTP {response.status}")
        return response.read()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-ableplayer-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "Managed" / "ableplayer-wwa.mp4"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen(
        [BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT
    )
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [
                str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        client = adaptive.connect_event_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        deadline = time.time() + 60
        loaded = None
        while time.time() < deadline:
            try:
                loaded = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
                if loaded.get("href") == PAGE and loaded.get("state") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Able Player page did not load: {loaded}")

        player = wait_ready(client)
        print("ABLEPLAYER-PLAYER-READY:", json.dumps(player, sort_keys=True), flush=True)
        click(client, player["playPoint"])
        print("ABLEPLAYER-PLAY-CLICK: trusted CDP input issued", flush=True)
        player = wait_playing(client)
        print("ABLEPLAYER-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        source = player["source"]
        source_parts = urlsplit(source)
        if source_parts.netloc.lower() != SOURCE_HOST or source_parts.path != SOURCE_PATH:
            raise RuntimeError(f"Able Player selected unexpected source: {source}")

        click(client, player["buttonPoint"])
        print("ABLEPLAYER-DOWNLOAD-CLICK: trusted CDP input issued", flush=True)
        db = public.db_path(str(home))
        job = public.wait_job(db, source, timeout=90)
        print("ABLEPLAYER-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "provisional": job.get("provisional"), "name": job.get("name")}, sort_keys=True), flush=True)
        if job.get("media") is not True or job.get("source") != source:
            raise RuntimeError(f"wrong Able Player native job: {job}")
        if len(public.jobs(db)) != 1:
            raise RuntimeError(f"unexpected extra Able Player job: {public.jobs(db)}")

        reference = independent_reference(source)
        print("ABLEPLAYER-INDEPENDENT-REFERENCE:", json.dumps({"url": redacted_url(source), "bytes": len(reference), "sha256": sha256_bytes(reference)}, sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], "ableplayer-wwa.webm", str(managed))
        done = public.wait_completed(db, job["id"], timeout=300)
        output = managed.read_bytes()
        output_hash = sha256_bytes(output)
        reference_hash = sha256_bytes(reference)
        if len(output) != len(reference) or output_hash != reference_hash:
            raise RuntimeError(f"resident media differs from independent reference: output={len(output)}/{output_hash} reference={len(reference)}/{reference_hash}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(public.jobs(db)) == 1, public.jobs(db)
        assert not browser_files, browser_files
        print(f"ABLEPLAYER: PASS (page={PAGE}, source={redacted_url(source)}, output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("ABLEPLAYER-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_ABLE") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ABLEPLAYER-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
