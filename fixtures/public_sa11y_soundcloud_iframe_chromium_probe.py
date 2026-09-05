#!/usr/bin/env python3
"""Real Chromium + Sa11y SoundCloud iframe public-player media initiation proof.

The official Sa11y SoundCloud iframe demo uses the documented HTML5 ``<source>`` form and a
public Oceans MP4. It uses a fresh Chromium profile, the real unpacked
extension/native host, a resident real binary, and ...[truncated]
resident binary. The fixture records browser network evidence without
persisting signed query values, clicks the product's player-bound Download
button, commits through the resident single-instance CLI, and compares the
native result with browser-side bytes.
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
from urllib.parse import parse_qsl, urlsplit, urlunsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_dash_quality_probe as adaptive
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
VIDEO_PAGE = "https://panzi.github.io/embedplayer/"
PAGE = VIDEO_PAGE


def redacted_url(url: str) -> str:
    """Keep host/path and query keys, never signed query values."""
    parts = urlsplit(url)
    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    query = "&".join(f"{key}=[REDACTED]" for key in keys)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_in_context(client: adaptive.EventCDP, context_id: int, expression: str):
    result = client.call("Runtime.evaluate", {"expression": expression, "contextId": context_id, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in result:
        raise RuntimeError(f"child Runtime.evaluate exception: {result['exceptionDetails']}")
    remote = result.get("result", {})
    return remote.get("value", remote.get("description"))


def browser_reference(client: adaptive.EventCDP, url: str, context_id: int) -> dict:
    raw = evaluate_in_context(
        client,
        context_id,
        "(async()=>{"
        f"const u={json.dumps(url)};const r=await fetch(u,{{cache:'no-store'}});"
        "if(!r.ok)return JSON.stringify({error:'HTTP '+r.status});"
        "const b=new Uint8Array(await r.arrayBuffer());"
        "const d=new Uint8Array(await crypto.subtle.digest('SHA-256',b));"
        "return JSON.stringify({size:b.length,hash:Array.from(d).map(x=>x.toString(16).padStart(2,'0')).join('')});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser reference fetch failed for {redacted_url(url)}: {result['error']}")
    return result


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
    raise RuntimeError(f"Sa11y SoundCloud iframe capture created no native media job for {source!r}: {last}")


def collect_execution_contexts(client: adaptive.EventCDP, contexts: dict[int, dict]) -> None:
    for event in client.take_events():
        if event.get("method") != "Runtime.executionContextCreated":
            continue
        context = event.get("params", {}).get("context", {})
        aux = context.get("auxData", {})
        context_id = context.get("id")
        frame_id = aux.get("frameId")
        if isinstance(context_id, int) and isinstance(frame_id, str) and aux.get("isDefault", True):
            contexts[context_id] = {"frameId": frame_id, "origin": context.get("origin", "")}


def frame_tree(client: adaptive.EventCDP) -> dict[str, dict]:
    result = client.call("Page.getFrameTree")
    frames: dict[str, dict] = {}
    def visit(node: dict, parent: str | None = None) -> None:
        frame = node.get("frame", {})
        frame_id = frame.get("id")
        if isinstance(frame_id, str):
            frames[frame_id] = {"url": frame.get("url", ""), "parent": parent}
            for child in node.get("childFrames", []) or []:
                visit(child, frame_id)
    visit(result.get("frameTree", {}))
    return frames


def top_iframe_rects(client: adaptive.EventCDP) -> list[dict]:
    raw = client.evaluate(
        "JSON.stringify(Array.from(document.querySelectorAll('iframe')).map((frame,index)=>{"
        "const box=frame.getBoundingClientRect();return {index,src:frame.src,top:box.top,left:box.left,right:box.right,bottom:box.bottom,width:box.width,height:box.height};}))"
    )
    return json.loads(raw)


def wait_player(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    contexts: dict[int, dict] = {}
    frames: dict[str, dict] = {}
    last = None
    last_error = None
    while time.time() < deadline:
        collect_execution_contexts(client, contexts)
        try:
            frames = frame_tree(client)
            client.evaluate("Array.from(document.querySelectorAll('iframe')).filter(frame=>frame.src.includes('w.soundcloud.com')).forEach(frame=>frame.scrollIntoView({block:'center'}));'scrolled'")
            iframes = top_iframe_rects(client)
            for context_id, context in list(contexts.items()):
                frame_id = context["frameId"]
                frame = frames.get(frame_id, {})
                frame_url = frame.get("url", "")
                if not frame_url.startswith("https://w.soundcloud.com/"):
                    continue
                state_raw = evaluate_in_context(
                    client,
                    context_id,
                    "JSON.stringify((()=>{"
                    "const media=Array.from(document.querySelectorAll('audio,video'));"
                    "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
                    "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
                    "const item=media.find(candidate=>sourceOf(candidate)&&visible(candidate))||media.find(candidate=>sourceOf(candidate));"
                    "if(item){item.muted=true;const play=item.play();if(play)play.catch(()=>{});}"
                    "const button=document.querySelector('#dm-media-download-button');const buttonBox=button?.getBoundingClientRect();"
                    "return {frameUrl:location.href,source:item?sourceOf(item):'',readyState:item?.readyState||0,paused:item?.paused??true,ended:item?.ended??false,duration:item?.duration??0,media:media.length,visible:item?visible(item):false,button:!!button,buttonRect:buttonBox?{top:buttonBox.top,left:buttonBox.left,right:buttonBox.right,bottom:buttonBox.bottom,width:buttonBox.width,height:buttonBox.height}:null};})())"
                )
                state = json.loads(state_raw)
                last = {"contextId": context_id, "frameId": frame_id, "frameUrl": frame_url, "state": state}
                source = state.get("source", "")
                matching_iframe = next((item for item in iframes if item.get("src") and (item["src"] == frame_url or frame_url.startswith(item["src"]) or item["src"].startswith(frame_url))), None)
                if source.startswith(("http://", "https://")) and state.get("readyState", 0) >= 2 and not state.get("paused") and state.get("button") and matching_iframe and state.get("buttonRect"):
                    button_box = state["buttonRect"]
                    return {**state, "contextId": context_id, "frameId": frame_id, "frameUrl": frame_url, "iframe": matching_iframe, "clickPoint": {"x": matching_iframe["left"] + button_box["left"] + button_box["width"] / 2, "y": matching_iframe["top"] + button_box["top"] + button_box["height"] / 2}, "frameChain": [frame_url]}
        except Exception as error:
            last_error = repr(error)
        time.sleep(0.5)
    raise RuntimeError(f"Sa11y SoundCloud iframe player did not become playable/injected: last={last}; error={last_error}; contexts={contexts}; frames={frames}")


def activate_player(client: adaptive.EventCDP) -> None:
    deadline = time.time() + 30
    point = None
    raw = 'null'
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const media=Array.from(document.querySelectorAll('audio,video'));"
            "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
            "const v=media.filter(visible).sort((a,b)=>b.getBoundingClientRect().width*b.getBoundingClientRect().height-a.getBoundingClientRect().width*a.getBoundingClientRect().height)[0];"
            "const r=v?.getBoundingClientRect();"
            "return r?{x:r.left+r.width/2,y:r.top+r.height/2}:null;})())"
        )
        point = json.loads(raw)
        if point:
            break
        time.sleep(0.5)
    if not point:
        raise RuntimeError(f"Sa11y SoundCloud iframe player surface missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + 25
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const v=document.querySelector('#mwe_player_0');"
            "const source=v?.currentSrc||v?.src||v?.querySelector('source[src]')?.getAttribute('src')||'';"
            "if(v&&source){v.muted=true;void v.play();}"
            "return {source,disabled:v?.hasAttribute('disabled')??true,readyState:v?.readyState||0};})())"
        )
        state = json.loads(raw)
        if state.get("source") and not state.get("disabled"):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Sa11y SoundCloud iframe player did not instantiate after surface click: {raw}")


def collect_media_events(client: adaptive.EventCDP) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for event in client.take_events():
        if event.get("method") != "Network.requestWillBeSent":
            continue
        params = event.get("params", {})
        request = params.get("request", {})
        url = request.get("url", "")
        host = urlsplit(url).netloc.lower()
        if "w.soundcloud.com" not in host:
            continue
        if not urlsplit(url).path.lower().endswith((".ogg", ".mp3", ".wav")):
            continue
        key = (url, str(params.get("requestId")))
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "url": redacted_url(url),
            "host": host,
            "type": request.get("method"),
            "range": next((value for key, value in request.get("headers", {}).items() if str(key).lower() == "range"), None),
            "requestId": params.get("requestId"),
        })
    return records


def wait_for_traffic(client: adaptive.EventCDP, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    records: list[dict] = []
    while time.time() < deadline:
        records.extend(collect_media_events(client))
        if records:
            return records
        time.sleep(0.5)
    return records


def button_point(player: dict) -> dict:
    point = player.get("clickPoint")
    if not point:
        raise RuntimeError(f"child-frame Download button has no page coordinates: {player}")
    return {"x": point["x"], "y": point["y"], "text": "Download", "frameUrl": player.get("frameUrl"), "source": player.get("source")}


def select_soundcloud(client: adaptive.EventCDP, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.evaluate("JSON.stringify(!!document.querySelector('#embed'))") == "true":
            client.evaluate("(()=>{const s=document.querySelector('#embed');const option=Array.from(s.options).find(item=>item.value.startsWith('iframe|https://w.soundcloud.com/'));if(!option)throw new Error('SoundCloud option missing');s.value=option.value;s.dispatchEvent(new Event('change',{bubbles:true}));return s.value;})()")
            return
        time.sleep(0.25)
    raise RuntimeError("Embed Player selector did not become available")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-sa11y-soundcloud-iframe-chromium-"))
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
        client.call("Page.navigate", {"url": PAGE})
        select_soundcloud(client)
        player = wait_player(client)
        print("AUDIO-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        assert player.get("frameUrl") and player["frameUrl"] != PAGE, player
        assert player.get("frameChain"), player
        assert player["frameUrl"].startswith("https://w.soundcloud.com/"), player
        initial_traffic = wait_for_traffic(client)
        print("AUDIO-TRAFFIC:", json.dumps(initial_traffic[:12], sort_keys=True), flush=True)
        point = button_point(player)
        print("AUDIO-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_media_job(db, player["source"])
        suffix = Path(urlsplit(job["source"]).path).suffix or ".audio"
        media_name = "sa11y-soundcloud-iframe" + suffix
        managed = managed_dir / media_name
        print("AUDIO-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "name": job.get("name"), "selected_segments": [redacted_url(url) for url in job.get("selected_segments", [])]}, sort_keys=True), flush=True)
        traffic = initial_traffic + collect_media_events(client)
        print("AUDIO-SELECTED-TRAFFIC:", json.dumps(traffic[:24], sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        browser_reference_result = None
        if job.get("source", "").startswith(("http://", "https://")):
            browser_reference_result = browser_reference(client, job["source"], player["contextId"])
            print("BROWSER-AUDIO-REFERENCE:", json.dumps({"url": redacted_url(job["source"]), **browser_reference_result}, sort_keys=True), flush=True)
            if native_size != int(browser_reference_result["size"]) or native_hash != browser_reference_result["hash"]:
                raise RuntimeError(f"native output differs from browser source: native={native_size}/{native_hash} browser={browser_reference_result}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        print("AUDIO-NATIVE-RESULT:", json.dumps({"state": done["state"], "provisional": done.get("provisional"), "bytes": native_size, "sha256": native_hash, "browser_downloads": browser_files}, sort_keys=True), flush=True)
        print(f"AUDIO-CHROMIUM: PASS (page={PAGE}, source={redacted_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, traffic={len(traffic)}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("AUDIO-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_SA11Y_SOUNDCLOUD_IFRAME") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"AUDIO-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
