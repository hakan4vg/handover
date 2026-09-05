#!/usr/bin/env python3
"""Real Chromium/native proof for the MDN interactive audio example opened directly."""
from __future__ import annotations

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
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://interactive-examples.mdn.mozilla.net/pages/tabbed/audio.html"


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_media_job(db: str, source: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if job.get("media") is True and job.get("source") == source]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"MDN interactive audio created no native media job: {last}")


def collect_contexts(client: adaptive.EventCDP, contexts: dict[int, dict]) -> None:
    for event in client.take_events():
        if event.get("method") != "Runtime.executionContextCreated":
            continue
        context = event.get("params", {}).get("context", {})
        aux = context.get("auxData", {})
        context_id = context.get("id")
        frame_id = aux.get("frameId")
        if isinstance(context_id, int) and isinstance(frame_id, str) and aux.get("isDefault", True):
            contexts[context_id] = {"frameId": frame_id, "origin": context.get("origin", "")}


def evaluate_in_context(client: adaptive.EventCDP, context_id: int, expression: str):
    result = client.call("Runtime.evaluate", {"expression": expression, "contextId": context_id, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in result:
        raise RuntimeError(f"child Runtime.evaluate exception: {result['exceptionDetails']}")
    remote = result.get("result", {})
    return remote.get("value", remote.get("description"))


def wait_context_player(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    contexts: dict[int, dict] = {}
    last = None
    while time.time() < deadline:
        collect_contexts(client, contexts)
        for context_id, context in list(contexts.items()):
            if not context["origin"].startswith("https://interactive-examples.mdn.mozilla.net"):
                continue
            try:
                raw = evaluate_in_context(
                    client,
                    context_id,
                    "JSON.stringify((()=>{"
                    "const media=Array.from(document.querySelectorAll('audio,video'));"
                    "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
                    "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
                    "const item=media.find(candidate=>sourceOf(candidate)&&visible(candidate))||media.find(candidate=>sourceOf(candidate));"
                    "if(item){item.muted=true;const play=item.play();if(play)play.catch(()=>{});}"
                    "const button=document.querySelector('#dm-media-download-button');const buttonBox=button?.getBoundingClientRect();"
                    "const owner=window.frameElement;const ownerBox=owner?.getBoundingClientRect();"
                    "return {frameUrl:location.href,referrer:document.referrer,source:item?sourceOf(item):'',readyState:item?.readyState||0,paused:item?.paused??true,duration:item?.duration??0,media:media.length,visible:item?visible(item):false,button:!!button,buttonRect:buttonBox?{top:buttonBox.top,left:buttonBox.left,right:buttonBox.right,bottom:buttonBox.bottom,width:buttonBox.width,height:buttonBox.height}:null,owner:owner?{src:owner.getAttribute('src')||'',width:ownerBox?.width||0,height:ownerBox?.height||0,top:ownerBox?.top||0,left:ownerBox?.left||0}:null};})())"
                )
                state = json.loads(raw)
                last = {"contextId": context_id, "frameId": context["frameId"], "origin": context["origin"], "state": state}
                if state.get("source") and state.get("readyState", 0) >= 2 and not state.get("paused") and state.get("button") and state.get("buttonRect") and state.get("owner"):
                    owner = state["owner"]
                    box = state["buttonRect"]
                    return {**state, "contextId": context_id, "frameId": context["frameId"], "contextOrigin": context["origin"], "clickPoint": {"x": owner["left"] + box["left"] + box["width"] / 2, "y": owner["top"] + box["top"] + box["height"] / 2}}
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError(f"MDN interactive context player did not become playable/injected: last={last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-mdn-interactive-audio-"))
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
            [
                str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
        client = adaptive.connect_event_chrome(chrome_port)
        public.wait_page(client, PAGE)
        player = wait_context_player(client)
        print("AUDIO-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        point = player["clickPoint"]
        print("AUDIO-BUTTON:", json.dumps({"x": point["x"], "y": point["y"], "trustedInput": True}, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_media_job(db, player["source"])
        assert job.get("source") == player["source"], job
        assert job.get("media") is True, job
        managed_name = "mdn-interactive-audio" + (Path(urlsplit(job["source"]).path).suffix or ".audio")
        managed = managed_dir / managed_name
        print("AUDIO-NATIVE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "media": job.get("media"), "state": job.get("state"), "provisional": job.get("provisional"), "name": job.get("name")}, sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], managed_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = support.sha256(str(managed))
        browser_size, browser_hash = public.sha256_browser(client, job["source"])
        print("BROWSER-AUDIO-REFERENCE:", json.dumps({"url": job["source"], "size": browser_size, "hash": browser_hash}, sort_keys=True), flush=True)
        assert native_size == browser_size, (native_size, browser_size, done)
        assert native_hash == browser_hash, (native_hash, browser_hash, done)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        print(f"MDN-INTERACTIVE-AUDIO: PASS (page={PAGE}, source={job['source']}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("MDN-INTERACTIVE-AUDIO-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_MDN_INTERACTIVE") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MDN-INTERACTIVE-AUDIO-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
