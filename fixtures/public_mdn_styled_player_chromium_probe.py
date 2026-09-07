#!/usr/bin/env python3
"""Real Chromium + Mozilla cross-browser HTML5 player media proof.

The official Mozilla demo uses a top-level HTML5 ``<video>`` with three
``<source>`` children and a public Tears of Steel MP4. It uses a fresh
Chromium profile, the real unpacked extension/native host, a resident real
binary, and the fixture records browser
network evidence without persisting query values, clicks the product's
player-bound Download button, commits through the resident single-instance
CLI, and compares the native result with browser-side bytes.
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
VIDEO_PAGE = "https://iandevlin.github.io/mdn/video-player-styled/"
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
    parts = urlsplit(url)
    reference_url = urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
    raw = client.evaluate(
        "(async()=>{"
        f"const u={json.dumps(reference_url)};const r=await fetch(u,{{cache:'no-store'}});"
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
    raise RuntimeError(f"custom video player capture created no native media job: {last}")


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
                "if(v){v.muted=true;}"
                "const r=v?.getBoundingClientRect();"
                "return {title:document.title,videoCount:media.length,videos:media.map(item=>({src:sourceOf(item),readyState:item.readyState,paused:item.paused,ended:item.ended,rect:(()=>{const box=item.getBoundingClientRect();return {top:box.top,left:box.left,right:box.right,bottom:box.bottom,width:box.width,height:box.height}})()})),"
                "src:v?sourceOf(v):'',readyState:v?.readyState||0,paused:v?.paused??true,"
                "ended:v?.ended??false,duration:v?.duration??0,error:v?.error?.message||null,"
                "button:!!document.querySelector('#dm-media-download-button'),customControl:!!document.querySelector('#playpause'),"
                "rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null,"
                "body:document.body?.innerText?.slice(0,400)||''};})())"
            )
            last_raw = repr(raw)
            last = json.loads(raw)
            if last.get("videoCount", 0) and last.get("src") and last.get("readyState", 0) >= 1 and last.get("customControl"):
                return last
        except Exception as error:
            last_error = repr(error)
        time.sleep(0.5)
    diagnostic = None
    try:
        diagnostic = client.evaluate("JSON.stringify({href:location.href,title:document.title,readyState:document.readyState,body:document.body?.innerText?.slice(0,800)||'',videos:document.querySelectorAll('audio,video').length,videoHtml:Array.from(document.querySelectorAll('audio,video')).map(v=>v.outerHTML.slice(0,1200)),sources:Array.from(document.querySelectorAll('audio source,video source')).map(s=>({attr:s.getAttribute('src'),prop:s.src,type:s.getAttribute('type')})),iframes:Array.from(document.querySelectorAll('iframe')).map(frame=>frame.src).slice(0,8)})")
    except Exception as error:
        diagnostic = repr(error)
    raise RuntimeError(f"custom video player did not become playable/injected: last={last}; raw={last_raw}; error={last_error}; diagnostic={diagnostic}")


def trusted_click(client: adaptive.EventCDP, selector: str) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{const b=document.querySelector(" + json.dumps(selector) + ");"
        "if(!b)throw Error('control missing: '+" + json.dumps(selector) + ");"
        "b.scrollIntoView({block:'center',inline:'center'});"
        "const r=b.getBoundingClientRect();const h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {selector:" + json.dumps(selector) + ",x:r.left+r.width/2,y:r.top+r.height/2,rect:{left:r.left,top:r.top,width:r.width,height:r.height},hit:{id:h?.id||'',tag:h?.tagName||'',text:h?.textContent?.trim()||''}};})())"
    )
    point = json.loads(raw)
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    return point


def activate_custom_player(client: adaptive.EventCDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const b=document.querySelector('#playpause');const v=document.querySelector('#video');"
        "if(!b||!v)return {button:!!b,video:!!v};"
        "const r=b.getBoundingClientRect();"
        "return {button:true,video:true,paused:v.paused,readyState:v.readyState,text:document.querySelector('#playpause-text')?.textContent?.trim()||'',x:r.left+r.width/2,y:r.top+r.height/2};"
        "})())"
    )
    before = json.loads(raw)
    if not before.get("button") or not before.get("video") or before.get("x") is None:
        raise RuntimeError(f"custom play/pause control missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": before["x"], "y": before["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": before["x"], "y": before["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + 45
    last = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{const v=document.querySelector('#video');const b=document.querySelector('#dm-media-download-button');return {paused:v?.paused??true,readyState:v?.readyState||0,currentTime:v?.currentTime||0,button:!!b,text:document.querySelector('#playpause-text')?.textContent?.trim()||''};})())"
        )
        last = json.loads(raw)
        if last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
            client.evaluate("(()=>{window.__dmMuteClicks=[];const b=document.querySelector('#mute');b.addEventListener('click',e=>window.__dmMuteClicks.push({trusted:e.isTrusted,target:e.target.id}),true);return 'armed';})()")
            mute_point = trusted_click(client, "#mute")
            mute_deadline = time.time() + 15
            mute = None
            while time.time() < mute_deadline:
                mute = json.loads(client.evaluate("JSON.stringify({muted:!!document.querySelector('#video')?.muted,label:document.querySelector('#mute')?.textContent?.trim()||'',dataState:document.querySelector('#mute')?.getAttribute('data-state')||''})"))
                if mute.get("muted"):
                    break
                time.sleep(0.25)
            mute_supported = bool(mute and mute.get("muted"))
            if not mute_supported:
                print("MDN-STYLED-MUTE-BOUNDARY:", json.dumps({"trusted_click": True, "muted": mute.get("muted") if mute else None, "label": mute.get("label") if mute else None, "data_state": mute.get("dataState") if mute else None, "note": "page trusted-input handler did not update media state; capture continues"}, sort_keys=True), flush=True)
            return {"before": before, "after": last, "trustedClick": True, "muteClick": mute_point, "mute": mute, "muteSupported": mute_supported}
        time.sleep(0.5)
    raise RuntimeError(f"custom play/pause click did not start/inject player: {last}")


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
        if "iandevlin.github.io" not in host:
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


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-mdn-styled-player-chromium-"))
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
        player = wait_player(client)
        print("MDN-STYLED-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        custom = activate_custom_player(client)
        print("MDN-STYLED-CONTROL:", json.dumps(custom, sort_keys=True), flush=True)
        initial_traffic = wait_for_traffic(client)
        print("MDN-STYLED-TRAFFIC:", json.dumps(initial_traffic[:12], sort_keys=True), flush=True)
        point = button_point(client)
        print("MDN-STYLED-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_media_job(db)
        suffix = Path(urlsplit(job["source"]).path).suffix or ".media"
        media_name = "mdn-styled-tears-of-steel" + suffix
        managed = managed_dir / media_name
        print("MDN-STYLED-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "name": job.get("name"), "selected_segments": [redacted_url(url) for url in job.get("selected_segments", [])]}, sort_keys=True), flush=True)
        traffic = initial_traffic + collect_media_events(client)
        print("MDN-STYLED-SELECTED-TRAFFIC:", json.dumps(traffic[:24], sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        browser_reference_result = None
        if job.get("source", "").startswith(("http://", "https://")):
            client.call("Page.navigate", {"url": "https://iandevlin.github.io/"})
            time.sleep(2)
            browser_reference_result = browser_reference(client, job["source"])
            print("BROWSER-MDN-STYLED-REFERENCE:", json.dumps({"url": redacted_url(job["source"]), **browser_reference_result}, sort_keys=True), flush=True)
            if native_size != int(browser_reference_result["size"]) or native_hash != browser_reference_result["hash"]:
                raise RuntimeError(f"native output differs from browser source: native={native_size}/{native_hash} browser={browser_reference_result}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        print("MDN-STYLED-NATIVE-RESULT:", json.dumps({"state": done["state"], "provisional": done.get("provisional"), "bytes": native_size, "sha256": native_hash, "browser_downloads": browser_files}, sort_keys=True), flush=True)
        print(f"MDN-STYLED-HTML5-CHROMIUM: PASS (page={PAGE}, source={redacted_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, traffic={len(traffic)}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("MDN-STYLED-HTML5-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_MDN_STYLED") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MDN-STYLED-HTML5-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
