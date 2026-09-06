#!/usr/bin/env python3
"""Real Chromium + drop-player official progressive fallback proof.

The official drop-player page exposes a real Quality -> Original control. This
fixture uses that player control to leave the blob-backed HLS path, captures the
page-provided progressive MP4, commits through the resident application, and
compares the managed bytes with an independent browser-context fetch.
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

PAGE = "https://player.drop.mov/"
ORIGINAL_SOURCE = "https://assets.drop.mov/samples/video/01/source.mp4"
BIN = public.BIN
EXTENSION = public.EXTENSION


def redacted_url(url: str) -> str:
    """Keep the URL shape and query keys; never retain signed values."""
    parts = urlsplit(url or "")
    if not parts.scheme:
        return url or ""
    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    query = "&".join(f"{key}=[REDACTED]" for key in keys)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def browser_reference(client: adaptive.EventCDP, url: str) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const u={json.dumps(url)};const r=await fetch(u,{{cache:'no-store'}});"
        "if(!r.ok)return JSON.stringify({error:'HTTP '+r.status});"
        "const b=new Uint8Array(await r.arrayBuffer());"
        "const d=new Uint8Array(await crypto.subtle.digest('SHA-256',b));"
        "return JSON.stringify({size:b.length,hash:Array.from(d).map(x=>x.toString(16).padStart(2,'0')).join(''),status:r.status,type:r.headers.get('content-type')||''});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"DPlayer browser reference failed for {redacted_url(url)}: {result['error']}")
    return result


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_media_job(db: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if job.get("media") is True]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"DPlayer capture created no native media job: {last}")


def wait_player(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    reload_at = time.time() + 12.0
    reloads = 0
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=Array.from(document.querySelectorAll('video'));"
                "const sourceOf=item=>item.currentSrc||item.src||item.getAttribute('src')||'';"
                "const v=media.find(item=>sourceOf(item))||media[0];"
                "if(v){v.scrollIntoView({block:'center'});v.muted=true;void v.play().catch(()=>{});}"
                "const r=v?.getBoundingClientRect();"
                "return {title:document.title,videoCount:media.length,src:v?sourceOf(v):'',"
                "readyState:v?.readyState||0,networkState:v?.networkState||0,paused:v?.paused??true,"
                "ended:v?.ended??false,duration:v?.duration??0,error:v?.error?{code:v.error.code,message:v.error.message}:null,"
                "button:!!document.querySelector('#dm-media-download-button'),"
                "rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null};})())"
            )
            last = json.loads(raw)
            if last.get("videoCount", 0) and last.get("readyState", 0) >= 2 and not last.get("paused") and not last.get("error") and last.get("button"):
                return last
        except Exception:
            pass
        if last and not last.get("button") and reloads < 1 and time.time() >= reload_at:
            client.call("Page.reload", {"ignoreCache": True})
            reloads += 1
            reload_at = time.time() + 12.0
            time.sleep(2.0)
        time.sleep(0.5)
    diagnostic = client.evaluate(
        "JSON.stringify({href:location.href,title:document.title,body:document.body?.innerText?.slice(0,800)||'',"
        "videos:Array.from(document.querySelectorAll('video')).map(v=>({src:v.currentSrc||v.src||'',readyState:v.readyState,paused:v.paused,networkState:v.networkState,error:v.error?{code:v.error.code,message:v.error.message}:null,html:v.outerHTML.slice(0,1200)})),"
        "button:!!document.querySelector('#dm-media-download-button')})"
    )
    raise RuntimeError(f"DPlayer player did not become playable/injected: last={last}; diagnostic={diagnostic}")


def select_original(client: adaptive.EventCDP, timeout: float = 60.0) -> dict:
    def dispatch(point: dict) -> None:
        client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})

    quality = json.loads(client.evaluate("""JSON.stringify((()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Quality'||x.getAttribute('aria-label')==='Quality');if(!b)throw Error('drop-player Quality button missing');const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})())"""))
    dispatch(quality)
    deadline = time.time() + 10
    choice = None
    while time.time() < deadline:
        raw = client.evaluate("""JSON.stringify((()=>{const b=[...document.querySelectorAll('[role=\"menuitem\"],button')].find(x=>x.textContent.trim()==='Original');if(!b)return null;const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})())""")
        choice = json.loads(raw)
        if choice:
            break
        time.sleep(0.2)
    if not choice:
        raise RuntimeError("drop-player Quality menu did not expose Original")
    dispatch(choice)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        raw = client.evaluate("""JSON.stringify((()=>{const v=document.querySelector('video');return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,error:v?.error?{code:v.error.code,message:v.error.message}:null,button:!!document.querySelector('#dm-media-download-button')}})())""")
        last = json.loads(raw)
        if last.get("src") == ORIGINAL_SOURCE and last.get("readyState", 0) >= 2 and not last.get("paused") and not last.get("error") and last.get("button"):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"drop-player Original source did not become playable/injected: {last}")


def safe_headers(headers: dict) -> dict:
    keep = {"content-type", "content-length", "content-range", "accept-ranges", "access-control-allow-origin", "location"}
    result = {}
    for key, value in headers.items():
        lowered = str(key).lower()
        if lowered in keep:
            result[lowered] = redacted_url(str(value)) if lowered == "location" else str(value)
    return result


def collect_network_evidence(client: adaptive.EventCDP) -> list[dict]:
    records: list[dict] = []
    request_urls: dict[str, str] = {}
    for event in client.take_events():
        method = event.get("method")
        params = event.get("params", {})
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            url = request.get("url", "")
            request_id = str(params.get("requestId"))
            request_urls[request_id] = url
            redirect = params.get("redirectResponse") or {}
            if any(marker in url.lower() for marker in ("dogecloud", "dogevideo", ".mp4")):
                records.append({
                    "event": "request",
                    "requestId": request_id,
                    "resourceType": params.get("type"),
                    "method": request.get("method"),
                    "url": redacted_url(url),
                    "redirect": ({"status": redirect.get("status"), "url": redacted_url(redirect.get("url", "")), "mimeType": redirect.get("mimeType"), "headers": safe_headers(redirect.get("headers", {}))} if redirect else None),
                })
        elif method == "Network.responseReceived":
            response = params.get("response", {})
            url = response.get("url", "")
            if any(marker in url.lower() for marker in ("dogecloud", "dogevideo", ".mp4")):
                records.append({
                    "event": "response",
                    "requestId": str(params.get("requestId")),
                    "resourceType": params.get("type"),
                    "url": redacted_url(url),
                    "status": response.get("status"),
                    "mimeType": response.get("mimeType"),
                    "headers": safe_headers(response.get("headers", {})),
                })
        elif method == "Network.loadingFailed":
            request_id = str(params.get("requestId"))
            url = request_urls.get(request_id, "")
            if any(marker in url.lower() for marker in ("dogecloud", "dogevideo", ".mp4")):
                records.append({
                    "event": "failed",
                    "requestId": request_id,
                    "resourceType": params.get("type"),
                    "url": redacted_url(url),
                    "errorText": params.get("errorText"),
                    "blockedReason": params.get("blockedReason"),
                    "canceled": params.get("canceled"),
                })
        elif method == "Network.loadingFinished":
            request_id = str(params.get("requestId"))
            url = request_urls.get(request_id, "")
            if any(marker in url.lower() for marker in ("dogecloud", "dogevideo", ".mp4")):
                records.append({"event": "finished", "requestId": request_id, "url": redacted_url(url), "encodedDataLength": params.get("encodedDataLength")})
    return records


def browser_ownership(client: adaptive.EventCDP, source: str) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "document.querySelector('#dm-dplayer-ownership-probe')?.remove();"
        f"const a=document.createElement('a');a.id='dm-dplayer-ownership-probe';a.href={json.dumps(source)};a.textContent='DPlayer ownership probe';"
        "a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';document.body.append(a);"
        "window.__dmDplayerOwnership={context:null,click:null};"
        "document.addEventListener('contextmenu',e=>{if(e.target.closest('#dm-dplayer-ownership-probe')){window.__dmDplayerOwnership.context={defaultPrevented:e.defaultPrevented,isTrusted:e.isTrusted,button:e.button};e.preventDefault();}},false);"
        "document.addEventListener('click',e=>{if(e.target.closest('#dm-dplayer-ownership-probe')){window.__dmDplayerOwnership.click={defaultPrevented:e.defaultPrevented,isTrusted:e.isTrusted,ctrlKey:e.ctrlKey,button:e.button};e.preventDefault();}},false);"
        "const r=a.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
    )
    point = json.loads(raw)
    for button, modifiers in (("right", 0), ("left", 2)):
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
    return json.loads(client.evaluate("JSON.stringify(window.__dmDplayerOwnership)"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-drop-player-original-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "Managed" / "drop-player-original.mp4"
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
        client = adaptive.connect_event_chrome(chrome_port)
        public.wait_page(client, PAGE)
        player = wait_player(client)
        print("DROP-ORIGINAL-INITIAL:", json.dumps({**player, "src": redacted_url(player.get("src", ""))}, sort_keys=True), flush=True)
        original = select_original(client)
        print("DROP-ORIGINAL-STATE:", json.dumps({**original, "src": redacted_url(original.get("src", ""))}, sort_keys=True), flush=True)
        assert original.get("src") == ORIGINAL_SOURCE and original.get("readyState", 0) >= 2 and not original.get("paused") and not original.get("error") and original.get("button"), original
        traffic = collect_network_evidence(client)
        print("DROP-ORIGINAL-NETWORK:", json.dumps(traffic, sort_keys=True), flush=True)
        assert any(item.get("event") == "request" and item.get("url") == ORIGINAL_SOURCE for item in traffic), traffic
        assert any(item.get("event") == "response" and item.get("url") == ORIGINAL_SOURCE and item.get("status") in {200, 206} for item in traffic), traffic
        assert not any(item.get("event") == "failed" and not item.get("canceled") for item in traffic), traffic

        point = json.loads(client.evaluate("JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');if(!b)throw Error('drop-player Original Download button missing');const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,text:b.textContent}})())"))
        print("DROP-ORIGINAL-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        button_event = client.evaluate("""(()=>{const item=document.querySelector('#dm-media-download-button');window.__dmDropOriginalButtonEvent=null;item.addEventListener('click',event=>{window.__dmDropOriginalButtonEvent={isTrusted:event.isTrusted,defaultPrevented:event.defaultPrevented,target:event.target?.id||event.target?.tagName||null};},{capture:true});return {pointerEvents:getComputedStyle(item).pointerEvents,top:(()=>{const r=item.getBoundingClientRect();const x=r.left+r.width/2,y=r.top+r.height/2;const el=document.elementFromPoint(x,y);return el?.id||el?.tagName||null})()};})()""")
        print("DROP-ORIGINAL-BUTTON-DIAGNOSTIC:", json.dumps(button_event, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        time.sleep(0.5)
        button_result = json.loads(client.evaluate("JSON.stringify(window.__dmDropOriginalButtonEvent)"))
        print("DROP-ORIGINAL-BUTTON-EVENT:", json.dumps(button_result, sort_keys=True), flush=True)
        assert button_result and button_result.get("isTrusted") is True and button_result.get("defaultPrevented") is False, button_result

        db = public.db_path(str(home))
        job = wait_media_job(db)
        job_source = job.get("source", "")
        print("DROP-ORIGINAL-NATIVE-JOB:", json.dumps({"id": job.get("id"), "source": redacted_url(job_source), "media": job.get("media"), "state": job.get("state"), "name": job.get("name")}, sort_keys=True), flush=True)
        assert job_source == ORIGINAL_SOURCE, {"expected": ORIGINAL_SOURCE, "actual": redacted_url(job_source)}
        destination = managed
        public.commit_via_cli(str(home), inspector_port, job["id"], "drop-player-original.mp4", str(destination))
        done = public.wait_completed(db, job["id"], timeout=300)
        reference = browser_reference(client, job_source)
        print("DROP-ORIGINAL-BROWSER-REFERENCE:", json.dumps({"url": redacted_url(job_source), **reference}, sort_keys=True), flush=True)
        native_size = destination.stat().st_size
        native_hash = sha256(destination)
        assert native_size == int(reference["size"]) and native_hash == reference["hash"], {"native": {"size": native_size, "hash": native_hash}, "reference": reference}

        ffprobe_run = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,sample_rate", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
        ffprobe = json.loads(ffprobe_run.stdout)
        video_streams = [stream for stream in ffprobe.get("streams", []) if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in ffprobe.get("streams", []) if stream.get("codec_type") == "audio"]
        assert video_streams and audio_streams and float(ffprobe.get("format", {}).get("duration", 0)) > 0, ffprobe
        ownership = browser_ownership(client, job_source)
        assert ownership.get("context") and ownership["context"].get("isTrusted") and not ownership["context"].get("defaultPrevented"), ownership
        assert ownership.get("click") and ownership["click"].get("isTrusted") and ownership["click"].get("ctrlKey") and not ownership["click"].get("defaultPrevented"), ownership
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        result = {"completed": {"state": done.get("state"), "provisional": done.get("provisional"), "id": done.get("id")}, "output_bytes": native_size, "output_sha256": native_hash, "ffprobe": ffprobe, "ownership": ownership, "browser_downloads": browser_files}
        print("DROP-ORIGINAL-NATIVE-RESULT:", json.dumps(result, sort_keys=True), flush=True)
        print(f"DROP-ORIGINAL-CHROMIUM: PASS (page={PAGE}, source={redacted_url(job_source)}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("DROP-ORIGINAL-CHROMIUM-PROBE: PASS", flush=True)
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
                chrome.kill(); chrome.wait(timeout=10)
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill(); app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill(); xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_DROP_PLAYER_ORIGINAL") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"DROP-ORIGINAL-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
