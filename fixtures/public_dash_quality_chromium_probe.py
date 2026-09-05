#!/usr/bin/env python3
"""Real Chromium blob/MSE DASH capture proof.

The DASH-IF reference client is driven through its visible MPD field and Load
button. It attaches a blob: MediaSource for a static 30-second public MPD.
The native manager selects the first representation in each adaptation set;
this probe fetches those exact browser-visible source tracks and checks their
sizes/hashes, then checks that native FFmpeg produced a playable two-track MP4.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import public_chromium_probe as public
import cdp_drive
import segmented_restart_probe as support
import hls_fmp4_probe as hls

BIN = public.BIN
EXTENSION = public.EXTENSION
MPD_SOURCE = "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps_shortened.mpd"
PAGE = "https://reference.dashif.org/dash.js/latest/samples/dash-if-reference-player/index.html"


def wait_page(client: cdp_drive.CDP, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
            if last.get("href", "").split("#", 1)[0] == PAGE and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public DASH page did not load: {last}")


def load_public_mpd(client: cdp_drive.CDP, timeout: float = 90.0) -> dict:
    client.evaluate(
        "(()=>{"
        f"const input=document.querySelector('#stream-url');input.value={json.dumps(MPD_SOURCE)};"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        "document.querySelector('#btn-load').click();return input.value;"
        "})()"
    )
    deadline = time.time() + timeout
    last = None
    last_error = None
    while time.time() < deadline:
        try:
            last = json.loads(
                client.evaluate(
                    "(async()=>{"
                    "const v=document.querySelector('video');"
                    "if(v){v.scrollIntoView({block:'center'});v.muted=true;try{await v.play()}catch{}}"
                    "const r=v?.getBoundingClientRect();"
                    "return JSON.stringify({src:v?.currentSrc||v?.src||'',blob:(v?.currentSrc||v?.src||'').startsWith('blob:'),readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,mpd:document.querySelector('#stream-url')?.value||'',button:!!document.querySelector('#dm-media-download-button'),rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null});"
                    "})()"
                )
            )
            if last.get("blob") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button") and last.get("mpd") == MPD_SOURCE:
                return last
        except Exception as error:
            last_error = repr(error)
        time.sleep(0.5)
    diagnostic = None
    try:
        diagnostic = client.evaluate("JSON.stringify({href:location.href,title:document.title,readyState:document.readyState,body:document.body?.innerText?.slice(0,1000)||'',video:document.querySelector('video')?.outerHTML?.slice(0,1600)||null,buttons:Array.from(document.querySelectorAll('button')).slice(0,30).map(x=>({text:x.textContent?.trim(),title:x.title,hidden:x.classList.contains('cb-hidden-element')}))})")
    except Exception as error:
        diagnostic = repr(error)
    raise RuntimeError(f"public DASH MSE player did not become playable/injected: last={last}; error={last_error}; diagnostic={diagnostic}")


def browser_dash_reference(client: cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const manifest={json.dumps(MPD_SOURCE)};"
        "const response=await fetch(manifest,{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status+' '+manifest);"
        "const xml=new DOMParser().parseFromString(await response.text(),'application/xml');"
        "if(xml.querySelector('parsererror'))throw Error('invalid MPD XML');"
        "const base=new URL(xml.querySelector('MPD>BaseURL')?.textContent.trim()||'./',manifest).href;"
        "const tracks=Array.from(xml.querySelectorAll('Period>AdaptationSet')).map(set=>{const rep=set.querySelector(':scope>Representation');const template=set.querySelector(':scope>SegmentTemplate')||set.querySelector('SegmentTemplate');return {kind:set.getAttribute('contentType')||set.getAttribute('mimeType')||'',id:rep?.getAttribute('id')||'',bandwidth:rep?.getAttribute('bandwidth')||'',template:{media:template?.getAttribute('media')||'',initialization:template?.getAttribute('initialization')||'',duration:Number(template?.getAttribute('duration')||0),timescale:Number(template?.getAttribute('timescale')||1),startNumber:Number(template?.getAttribute('startNumber')||1)}}});"
        "if(tracks.some(track=>!track.template.media||!track.template.initialization))throw Error('MPD lacked first-representation templates');"
        "const duration=30;"
        "const refs=tracks.map(track=>{const count=Math.ceil(duration*track.template.timescale/track.template.duration);const urls=[];const replace=value=>value.replaceAll('$RepresentationID$',track.id).replaceAll('$Bandwidth$',track.bandwidth);urls.push(new URL(replace(track.template.initialization),base).href);for(let i=0;i<count;i++)urls.push(new URL(replace(track.template.media).replaceAll('$Number$',String(track.template.startNumber+i)),base).href);return {...track,urls}});"
        "const fetchBytes=async url=>{const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status+' '+url);return new Uint8Array(await r.arrayBuffer())};"
        "const digest=async parts=>{const joined=new Uint8Array(parts.reduce((sum,part)=>sum+part.length,0));let offset=0;for(const part of parts){joined.set(part,offset);offset+=part.length}const hash=new Uint8Array(await crypto.subtle.digest('SHA-256',joined));return {size:joined.length,hash:Array.from(hash).map(value=>value.toString(16).padStart(2,'0')).join('')}};"
        "for(const track of refs){Object.assign(track,await digest(await Promise.all(track.urls.map(fetchBytes))));}"
        "return JSON.stringify({manifest,tracks:refs.map(track=>({kind:track.kind,id:track.id,bandwidth:track.bandwidth,segments:track.urls.length,size:track.size,hash:track.hash}))});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser DASH reference failed: {result['error']}")
    return result


def browser_ownership(client: cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-dash-save-as-probe');old?.remove();"
        "const a=document.createElement('a');a.id='dm-dash-save-as-probe';a.href='https://dashif.org/?dm_dash_ownership=1';a.textContent='Browser-owned link';a.style.cssText='position:fixed;top:24px;left:24px;z-index:2147483647;padding:12px;background:#fff;color:#000';document.body.append(a);"
        "window.__dmDashOwnership={context:null,click:null};"
        "document.addEventListener('contextmenu',event=>{if(event.target.closest('#dm-dash-save-as-probe')){window.__dmDashOwnership.context={defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,button:event.button};event.preventDefault();}},false);"
        "document.addEventListener('click',event=>{if(event.target.closest('#dm-dash-save-as-probe')){window.__dmDashOwnership.click={defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,ctrlKey:event.ctrlKey,button:event.button};event.preventDefault();}},false);"
        "const rect=a.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2};})())"
    )
    point = json.loads(raw)
    for button, modifiers in (("right", 0), ("left", 2)):
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
    return json.loads(client.evaluate("JSON.stringify(window.__dmDashOwnership)"))


def select_quality(client: cdp_drive.CDP, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    raw = 'null'
    control = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{const b=Array.from(document.querySelectorAll('button[title=\"Quality\"]')).find(x=>!x.classList.contains('cb-hidden-element'));"
            "if(!b)return {button:false};b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();"
            "return {button:true,x:r.left+r.width/2,y:r.top+r.height/2,title:b.title};})())"
        )
        state = json.loads(raw)
        if state.get("button") and state.get("x") is not None:
            control = state
            break
        time.sleep(0.5)
    if not control:
        raise RuntimeError(f"DASH quality control missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": control["x"], "y": control["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": control["x"], "y": control["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{const menu=Array.from(document.querySelectorAll('.cb-menu')).find(x=>!x.classList.contains('cb-hidden-element'));"
            "const items=menu?[...menu.querySelectorAll('.cb-menu-item')].filter(x=>/kbps/.test(x.textContent||'')&&!/^Auto$/.test((x.textContent||'').trim())):[];"
            "if(!items.length)return {menu:!!menu,items:[]};const target=items[0];target.scrollIntoView({block:'center'});const r=target.getBoundingClientRect();"
            "return {menu:true,items:items.map(x=>x.textContent.trim()),target:target.textContent.trim(),x:r.left+r.width/2,y:r.top+r.height/2};})())"
        )
        state = json.loads(raw)
        if state.get("target") and state.get("x") is not None:
            break
        time.sleep(0.5)
    if not state.get("target"):
        raise RuntimeError(f"DASH quality entries missing: {raw}")
    target = state
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": target["x"], "y": target["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": target["x"], "y": target["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + 30
    selected = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{const menu=Array.from(document.querySelectorAll('.cb-menu')).find(x=>!x.classList.contains('cb-hidden-element'));"
            "return {target:"+json.dumps(target["target"])+",selected:menu?[...menu.querySelectorAll('.cb-menu-item-selected')].map(x=>x.textContent.trim()):[]};})())"
        )
        selected = json.loads(raw)
        if target["target"] in selected.get("selected", []):
            return {"control": control, "target": target["target"], "items": target["items"], "selected": selected["selected"]}
        time.sleep(0.5)
    raise RuntimeError(f"DASH quality selection did not persist: {selected}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-dash-quality-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
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
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]
            + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        player = load_public_mpd(client)
        print("DASH-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        quality = select_quality(client)
        print("DASH-QUALITY:", json.dumps(quality, sort_keys=True), flush=True)
        button_point = json.loads(client.evaluate("JSON.stringify((()=>{const r=document.querySelector('#dm-media-download-button')?.getBoundingClientRect();if(!r)throw Error('media button disappeared');return {x:r.left+r.width/2,y:r.top+r.height/2,top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}})())"))
        print("DASH-BUTTON:", json.dumps(button_point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        created = public.wait_job(db, MPD_SOURCE, timeout=90)
        assert created.get("media") is True and created.get("source") == MPD_SOURCE, created
        destination = root / "Downloads" / "public-dash-quality-mse.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "public-dash-quality-mse.mp4", str(destination))
        reference = browser_dash_reference(client)
        print("BROWSER-DASH-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        assert reference["manifest"] == MPD_SOURCE and len(reference["tracks"]) == 2, reference
        completed = public.wait_completed(db, created["id"], timeout=180)
        output_size = destination.stat().st_size
        output_hash = support.sha256(str(destination))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
        ffprobe = json.loads(probe.stdout)
        stream_types = sorted(stream["codec_type"] for stream in ffprobe.get("streams", []))
        assert stream_types == ["audio", "video"], ffprobe
        assert 31.0 <= float(ffprobe["format"]["duration"]) <= 33.0, ffprobe
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        ownership = browser_ownership(client)
        assert ownership["context"] and not ownership["context"]["defaultPrevented"] and ownership["context"]["isTrusted"], ownership
        assert ownership["click"] and not ownership["click"]["defaultPrevented"] and ownership["click"]["isTrusted"] and ownership["click"]["ctrlKey"], ownership
        assert len(public.jobs(db)) == 1, public.jobs(db)
        print(f"BROWSER-OWNERSHIP: PASS ({json.dumps(ownership, sort_keys=True)})", flush=True)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print("PUBLIC-DASH-CHROMIUM: PASS " + f"(page={PAGE}, manifest={MPD_SOURCE}, player_src={player['src']}, tracks={json.dumps(reference['tracks'], sort_keys=True)}, output_bytes={output_size}, output_sha256={output_hash}, ffprobe={json.dumps(ffprobe, sort_keys=True)}, jobs={len(public.jobs(db))}, browser_downloads={browser_files})", flush=True)
        print("PUBLIC-DASH-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
        raise
    finally:
        if client is not None: client.sock.close()
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
        if os.environ.get("DM_KEEP_PUBLIC_DASH") != "1": shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-DASH-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
