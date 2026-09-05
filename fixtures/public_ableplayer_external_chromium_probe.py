#!/usr/bin/env python3
"""Real Chromium + Able Player external-control video proof.

The official Able Player external4 demo constructs an AblePlayer instance from
JavaScript without the data-able-player attribute and exposes a separate page
#play control. The probe clicks that external control with trusted Chromium
input, verifies the video is playing, then clicks the product Download button,
commits through the resident CLI, and compares native bytes with a browser
reference.
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
VIDEO_PAGE = "https://ableplayer.github.io/ableplayer/demos/external4.html"
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


def browser_reference(client: adaptive.EventCDP, url: str) -> dict:
    raw = client.evaluate(
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


def wait_media_job(db: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if job.get("media") is True]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"MediaElement player capture created no native media job: {last}")


def wait_player(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    last_raw = None
    last_error = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=Array.from(document.querySelectorAll('audio,video'));"
                "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
                "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
                "const v=media.find(item=>sourceOf(item)&&visible(item))||media.find(item=>sourceOf(item))||media[0];"
                "if(v){v.muted=true;void v.play();}"
                "const r=v?.getBoundingClientRect();"
                "return {title:document.title,videoCount:media.length,videos:media.map(item=>({src:sourceOf(item),readyState:item.readyState,paused:item.paused,ended:item.ended,rect:(()=>{const box=item.getBoundingClientRect();return {top:box.top,left:box.left,right:box.right,bottom:box.bottom,width:box.width,height:box.height}})()})),"
                "src:v?sourceOf(v):'',readyState:v?.readyState||0,paused:v?.paused??true,"
                "ended:v?.ended??false,duration:v?.duration??0,error:v?.error?.message||null,"
                "button:!!document.querySelector('#dm-media-download-button'),"
                "rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null,"
                "body:document.body?.innerText?.slice(0,400)||''};})())"
            )
            last_raw = repr(raw)
            last = json.loads(raw)
            if last.get("videoCount", 0) and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
                return last
        except Exception as error:
            last_error = repr(error)
        time.sleep(0.5)
    diagnostic = None
    try:
        diagnostic = client.evaluate("JSON.stringify({href:location.href,title:document.title,readyState:document.readyState,body:document.body?.innerText?.slice(0,800)||'',videos:document.querySelectorAll('audio,video').length,videoHtml:Array.from(document.querySelectorAll('audio,video')).map(v=>v.outerHTML.slice(0,1200)),sources:Array.from(document.querySelectorAll('audio source,video source')).map(s=>({attr:s.getAttribute('src'),prop:s.src,type:s.getAttribute('type')})),iframes:Array.from(document.querySelectorAll('iframe')).map(frame=>frame.src).slice(0,8)})")
    except Exception as error:
        diagnostic = repr(error)
    raise RuntimeError(f"MediaElement player did not become playable/injected: last={last}; raw={last_raw}; error={last_error}; diagnostic={diagnostic}")


def activate_player(client: adaptive.EventCDP) -> None:
    deadline = time.time() + 30
    point = None
    raw = 'null'
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const media=Array.from(document.querySelectorAll('video'));"
            "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
            "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
            "const v=media.find(item=>sourceOf(item)&&visible(item))||media.find(item=>sourceOf(item));"
            "if(!v)return null;v.scrollIntoView({block:'center'});const r=v.getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2,source:sourceOf(v),readyState:v.readyState,paused:v.paused};})())"
        )
        point = json.loads(raw)
        if point:
            break
        time.sleep(0.5)
    if not point:
        raise RuntimeError(f"MediaElement player surface missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + 25
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const media=Array.from(document.querySelectorAll('video'));"
            "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
            "const v=media.find(item=>sourceOf(item).toLowerCase().split('?')[0].endsWith('.mp4'));"
            "const source=v?sourceOf(v):'';"
            "if(v&&source){v.muted=true;void v.play();}"
            "return {source,disabled:v?.hasAttribute('disabled')??true,readyState:v?.readyState||0,paused:v?.paused??true};})())"
        )
        state = json.loads(raw)
        if state.get("source") and not state.get("disabled"):
            return
        time.sleep(0.5)
    raise RuntimeError(f"MediaElement player did not instantiate after surface click: {raw}")


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
        if host != "ableplayer.github.io":
            continue
        if not urlsplit(url).path.lower().endswith((".mp4", ".webm", ".ogg", ".mp3", ".wav")):
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


def button_point(client: adaptive.EventCDP) -> dict:
    return json.loads(client.evaluate(
        "JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');"
        "if(!b)throw Error('Download button missing');const r=b.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,text:b.textContent};})())"
    ))


def activate_external_play(client: adaptive.EventCDP, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    raw = 'null'
    control = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{const b=document.querySelector('#play');const r=b?.getBoundingClientRect();"
            "return b&&r?{button:true,text:b.textContent.trim(),x:r.left+r.width/2,y:r.top+r.height/2}: {button:false};})())"
        )
        state = json.loads(raw)
        if state.get("button") and state.get("x") is not None:
            control = state
            break
        time.sleep(0.5)
    if not control:
        raise RuntimeError(f"Able Player external Play control missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": control["x"], "y": control["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": control["x"], "y": control["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{const v=document.querySelector('video#video1');const r=v?.getBoundingClientRect();"
            "return {video:!!v,source:v?.currentSrc||v?.querySelector('source[src]')?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,currentTime:v?.currentTime||0,status:document.querySelector('#status-play')?.textContent||'',width:r?.width||0,height:r?.height||0};})())"
        )
        state = json.loads(raw)
        if state.get("source", "").endswith("/media/wwa.mp4") and state.get("readyState", 0) >= 2 and not state.get("paused") and state.get("status") == "true":
            return {"control": control, "video": state}
        time.sleep(0.5)
    raise RuntimeError(f"Able Player external play did not start video: {raw}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-ableplayer-external-chromium-"))
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
        external = activate_external_play(client)
        print("ABLEPLAYER-EXTERNAL-PLAY:", json.dumps(external, sort_keys=True), flush=True)
        player = wait_player(client)
        print("ABLEPLAYER-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        initial_traffic = wait_for_traffic(client)
        print("ABLEPLAYER-TRAFFIC:", json.dumps(initial_traffic[:12], sort_keys=True), flush=True)
        point = button_point(client)
        print("ABLEPLAYER-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_media_job(db)
        media_name = "ableplayer-external-wwa.mp4"
        managed = managed_dir / media_name
        print("ABLEPLAYER-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "name": job.get("name"), "selected_segments": [redacted_url(url) for url in job.get("selected_segments", [])]}, sort_keys=True), flush=True)
        traffic = initial_traffic + collect_media_events(client)
        print("ABLEPLAYER-SELECTED-TRAFFIC:", json.dumps(traffic[:24], sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        browser_reference_result = None
        if job.get("source", "").startswith(("http://", "https://")):
            reference_url = "https://ableplayer.github.io/ableplayer/media/wwa.mp4"
            client.call("Page.navigate", {"url": "https://ableplayer.github.io/"})
            time.sleep(2)
            browser_reference_result = browser_reference(client, reference_url)
            print("BROWSER-ABLEPLAYER-REFERENCE:", json.dumps({"url": redacted_url(reference_url), "job_source": redacted_url(job["source"]), **browser_reference_result}, sort_keys=True), flush=True)
            if native_size != int(browser_reference_result["size"]) or native_hash != browser_reference_result["hash"]:
                raise RuntimeError(f"native output differs from browser source: native={native_size}/{native_hash} browser={browser_reference_result}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        print("ABLEPLAYER-NATIVE-RESULT:", json.dumps({"state": done["state"], "provisional": done.get("provisional"), "bytes": native_size, "sha256": native_hash, "browser_downloads": browser_files}, sort_keys=True), flush=True)
        print(f"ABLEPLAYER-CHROMIUM: PASS (page={PAGE}, source={redacted_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, traffic={len(traffic)}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("ABLEPLAYER-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_ABLEPLAYER_EXTERNAL") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MEDIAELEMENT-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
