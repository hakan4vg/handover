#!/usr/bin/env python3
"""Real Chromium + IVID web-component progressive media proof.

The official IVID sandbox uses a light-DOM ``<i-video>`` custom element with
custom controls and a finite Internet Archive MP4. This probe interacts with
the real IVID play control through trusted Chromium input, waits for the
extension's player-bound Download control, and verifies resident output against
an independent fetch of the exact captured source.
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
PAGE = "https://ividjs.github.io/ivid/"
SOURCE_HOST_SUFFIX = ".us.archive.org"
SOURCE_PATH_FRAGMENT = "/items/arashyekt4_gmail_Cat/Cat.mp4"


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


def state(client: adaptive.EventCDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const host=document.querySelector('i-video');"
        "const video=host?.querySelector('video.ivid__video')||document.querySelector('video');"
        "const control=host?.querySelector('.ivid__ctrls-button[data-state]');"
        "const button=document.querySelector('#dm-media-download-button');"
        "const vr=video?.getBoundingClientRect();const cr=control?.getBoundingClientRect();const br=button?.getBoundingClientRect();"
        "return {host:!!host,source:video?(video.currentSrc||video.src||''):'',readyState:video?.readyState||0,paused:video?.paused??true,ended:video?.ended??false,duration:video?.duration??0,videoRect:vr?{top:vr.top,left:vr.left,right:vr.right,bottom:vr.bottom,width:vr.width,height:vr.height}:null,controlState:control?.getAttribute('data-state')||'',controlPoint:cr?{x:cr.left+cr.width/2,y:cr.top+cr.height/2,width:cr.width,height:cr.height}:null,button:!!button,buttonPoint:br?{x:br.left+br.width/2,y:br.top+br.height/2,width:br.width,height:br.height}:null,buttonPointerEvents:button?getComputedStyle(button).pointerEvents:null};})())"
    )
    return json.loads(raw)


def wait_ready(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            client.evaluate(
                "(()=>{const v=document.querySelector('i-video video.ivid__video')||document.querySelector('video');"
                "v?.scrollIntoView({block:'center',inline:'center'});return v?.currentSrc||v?.src||'';})()"
            )
            last = state(client)
            source = last.get("source", "")
            if (
                last.get("host")
                and urlsplit(source).netloc.lower().endswith(SOURCE_HOST_SUFFIX)
                and SOURCE_PATH_FRAGMENT in urlsplit(source).path
                and last.get("controlPoint")
            ):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    diagnostics = []
    for event in client.take_events():
        method = event.get("method")
        if method == "Runtime.exceptionThrown":
            details = event.get("params", {}).get("exceptionDetails", {})
            diagnostics.append({"method": method, "text": details.get("text"), "description": (details.get("exception") or {}).get("description")})
        elif method == "Log.entryAdded":
            entry = event.get("params", {}).get("entry", {})
            diagnostics.append({"method": method, "level": entry.get("level"), "text": entry.get("text")})
        elif method == "Network.loadingFailed":
            params = event.get("params", {})
            diagnostics.append({"method": method, "errorText": params.get("errorText"), "type": params.get("type")})
    raise RuntimeError(f"IVID player did not become ready: {last}; runtime_diagnostics={diagnostics[-20:]}")


def wait_playing(client: adaptive.EventCDP, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = state(client)
            if last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button") and last.get("buttonPoint"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"IVID player did not become playing/injected: {last}")


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
    root = Path(tempfile.mkdtemp(prefix="dm-ivid-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "Managed" / "ivid-cat.mp4"
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
            raise RuntimeError(f"IVID page did not load: {loaded}")

        player = wait_ready(client)
        print("IVID-PLAYER-READY:", json.dumps(player, sort_keys=True), flush=True)
        source = player["source"]
        if not source.startswith("https://"):
            raise RuntimeError(f"IVID source did not resolve to HTTPS: {source}")

        # The sandbox can autoplay, but the real IVID control must also be
        # exercised. If it is already playing, issue a trusted pause then a
        # trusted play; otherwise issue the trusted play directly.
        if not player.get("paused"):
            click(client, player["controlPoint"])
            pause_deadline = time.time() + 20
            while time.time() < pause_deadline and not state(client).get("paused"):
                time.sleep(0.2)
            player = state(client)
            print("IVID-PAUSE-CLICK: trusted CDP input issued", flush=True)
        click(client, player["controlPoint"])
        print("IVID-PLAY-CLICK: trusted CDP input issued", flush=True)
        player = wait_playing(client)
        print("IVID-PLAYER:", json.dumps(player, sort_keys=True), flush=True)

        click(client, player["buttonPoint"])
        print("IVID-DOWNLOAD-CLICK: trusted CDP input issued", flush=True)
        db = public.db_path(str(home))
        job = public.wait_job(db, source, timeout=90)
        print("IVID-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "provisional": job.get("provisional"), "name": job.get("name")}, sort_keys=True), flush=True)
        if job.get("media") is not True or job.get("source") != source:
            raise RuntimeError(f"wrong native job captured: {job}")

        reference = independent_reference(source)
        print("IVID-INDEPENDENT-REFERENCE:", json.dumps({"url": redacted_url(source), "bytes": len(reference), "sha256": sha256_bytes(reference)}, sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], "ivid-cat.mp4", str(managed))
        done = public.wait_completed(db, job["id"], timeout=360)
        output = managed.read_bytes()
        output_hash = sha256_bytes(output)
        reference_hash = sha256_bytes(reference)
        if len(output) != len(reference) or output_hash != reference_hash:
            raise RuntimeError(f"resident output differs from independent reference: output={len(output)}/{output_hash} reference={len(reference)}/{reference_hash}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(public.jobs(db)) == 1, public.jobs(db)
        assert not browser_files, browser_files
        print(f"IVID: PASS (page={PAGE}, source={redacted_url(source)}, output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("IVID-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_IVID") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"IVID-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
