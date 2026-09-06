#!/usr/bin/env python3
"""Real Chromium ReactPlayer shadow-DOM HLS capture proof.

The official ReactPlayer demo selects its real HLS (m3u8) control. ReactPlayer
mounts an open-shadow ``HLS-VIDEO`` element; the extension must associate that
playing shadow media with the public HLS master, start one native acquisition,
and preserve browser ownership. The browser reference follows the rendition
manifest actually observed in Chromium, never managed ordering.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import public_chromium_probe as public
import adaptive_dash_quality_probe as adaptive
import cdp_drive
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
HLS_SOURCE = "https://stream.mux.com/VcmKA6aqzIzlg3MayLJDnbF55kX00mds028Z65QxvBYaA.m3u8"
PAGE = "https://cookpete.github.io/react-player/"


def redacted_url(url: str) -> str:
    """Keep public host/path shape without signed query/path values."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path
    if "mux.com" in host and "/rendition.m3u8" in path:
        path = "/[REDACTED]/rendition.m3u8"
    elif "mux.com" in host and "/v1/chunk/" in path:
        path = "/v1/chunk/[REDACTED]"
    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    query = "&".join(f"{key}=[REDACTED]" for key in keys)
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def observed_renditions(client: adaptive.EventCDP) -> list[str]:
    urls: list[str] = []
    for event in client.take_events():
        if event.get("method") != "Network.requestWillBeSent":
            continue
        url = str(event.get("params", {}).get("request", {}).get("url", ""))
        parsed = urlsplit(url)
        if "mux.com" not in parsed.netloc.lower() or not parsed.path.lower().endswith(".m3u8") or url == HLS_SOURCE:
            continue
        if parsed.path.lower().endswith("/subtitles.m3u8"):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def wait_page(client: cdp_drive.CDP, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate("JSON.stringify({href:location.href,state:document.readyState})")
            last = json.loads(raw)
            if last.get("href", "").split("#", 1)[0] == PAGE and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public HLS page did not load: {last}")


def click_hls_control(client: cdp_drive.CDP, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const b=Array.from(document.querySelectorAll('button')).find(item=>item.textContent?.trim()==='HLS (m3u8)');"
            "if(!b)return {found:false};const r=b.getBoundingClientRect();"
            "return {found:true,x:r.left+r.width/2,y:r.top+r.height/2,text:b.textContent?.trim()};})())"
        )
        last = json.loads(raw)
        if last.get("found"):
            client.call("Input.dispatchMouseEvent", {"type":"mousePressed","x":last["x"],"y":last["y"],"button":"left","clickCount":1,"modifiers":0})
            client.call("Input.dispatchMouseEvent", {"type":"mouseReleased","x":last["x"],"y":last["y"],"button":"left","clickCount":1,"modifiers":0})
            return last
        time.sleep(0.5)
    raise RuntimeError(f"ReactPlayer HLS control missing: {last}")


def wait_hls_player(client: cdp_drive.CDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=[];const roots=[document];const seen=new Set();"
                "while(roots.length){const root=roots.shift();root.querySelectorAll('video,audio').forEach(item=>{if(!seen.has(item)){seen.add(item);media.push(item);}});root.querySelectorAll('*').forEach(item=>{if(item.shadowRoot)roots.push(item.shadowRoot);});}"
                "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
                "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
                "const v=media.find(item=>sourceOf(item)&&visible(item))||media.find(item=>sourceOf(item))||media[0];"
                "if(v){v.muted=true;void v.play();}const r=v?.getBoundingClientRect();"
                "return {src:v?.currentSrc||v?.src||'',blob:(v?.currentSrc||v?.src||'').startsWith('blob:'),readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,mediaCount:media.length,shadowMediaCount:media.filter(item=>item.getRootNode()!==document).length,button:!!document.querySelector('#dm-media-download-button'),rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null};})())"
            )
            last = json.loads(raw)
            if last.get("blob") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"ReactPlayer shadow HLS player did not become playable/injected: {last}")


def browser_hls_reference(client: cdp_drive.CDP, manifest: str, reference_root: Path) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"let manifest={json.dumps(manifest)};"
        "const bytes=async url=>{const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);return new Uint8Array(await response.arrayBuffer())};"
        "const text=new TextDecoder();const lines=raw=>text.decode(raw).split(/\\r?\\n/).map(line=>line.trim()).filter(Boolean);"
        "let playlist=lines(await bytes(manifest));"
        "if(playlist.some(line=>line.startsWith('#EXT-X-STREAM-INF'))){let pending=false;let next='';for(const line of playlist){if(line.startsWith('#EXT-X-STREAM-INF')){pending=true;continue}if(pending&&!line.startsWith('#')){next=new URL(line,manifest).href;break}}if(!next)throw Error('master playlist has no rendition');manifest=next;playlist=lines(await bytes(manifest));}"
        "const parts=[];const map=playlist.find(line=>line.startsWith('#EXT-X-MAP:'));if(map){const match=map.match(/URI=\"([^\"]+)\"/);if(!match)throw Error('HLS map URI missing');parts.push(await bytes(new URL(match[1],manifest).href));}"
        "const segments=playlist.filter(line=>!line.startsWith('#')).map(line=>new URL(line,manifest).href);if(!segments.length)throw Error('HLS playlist has no segments');"
        "for(const segment of segments)parts.push(await bytes(segment));"
        "const joined=new Uint8Array(parts.reduce((sum,part)=>sum+part.length,0));let offset=0;for(const part of parts){joined.set(part,offset);offset+=part.length}"
        "const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',joined));"
        "const encode=bytes=>{let text='';for(let index=0;index<bytes.length;index+=32768)text+=String.fromCharCode(...bytes.slice(index,index+32768));return btoa(text)};"
        "return JSON.stringify({manifest,segments:segments.length,size:joined.length,hash:Array.from(digest).map(value=>value.toString(16).padStart(2,'0')).join(''),parts:parts.map(encode)});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser HLS reference failed for {redacted_url(manifest)}: {result['error']}")
    result["manifest"] = redacted_url(result["manifest"])
    raw_parts = [base64.b64decode(part) for part in result.pop("parts", [])]
    if not raw_parts:
        raise RuntimeError("browser HLS reference returned no media bytes")
    input_path = reference_root / "browser-reference-input.mp4"
    input_path.write_bytes(b"".join(raw_parts))
    output_path = reference_root / "browser-reference-final.mp4"
    remux = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path), "-map", "0", "-c", "copy", str(output_path)], capture_output=True, text=True, check=False)
    if remux.returncode != 0:
        raise RuntimeError(f"browser reference remux failed: {remux.stderr.strip()}")
    result["raw_size"] = sum(len(part) for part in raw_parts)
    result["size"] = output_path.stat().st_size
    result["hash"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return result


def browser_ownership(client: cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-reactplayer-save-as-probe');old?.remove();"
        "const a=document.createElement('a');a.id='dm-reactplayer-save-as-probe';"
        "a.href='https://cookpete.github.io/react-player/?dm_reactplayer_ownership=1';"
        "a.textContent='Browser-owned link';"
        "a.style.cssText='position:fixed;top:24px;left:24px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);window.__dmReactPlayerOwnership={context:null,click:null};"
        "document.addEventListener('contextmenu',event=>{if(event.target.closest('#dm-reactplayer-save-as-probe')){window.__dmReactPlayerOwnership.context={defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,button:event.button};event.preventDefault();}},false);"
        "document.addEventListener('click',event=>{if(event.target.closest('#dm-reactplayer-save-as-probe')){window.__dmReactPlayerOwnership.click={defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,ctrlKey:event.ctrlKey,button:event.button};event.preventDefault();}},false);"
        "const rect=a.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2};})())"
    )
    point = json.loads(raw)
    for button, modifiers in (("right", 0), ("left", 2)):
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
    return json.loads(client.evaluate("JSON.stringify(window.__dmReactPlayerOwnership)"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-reactplayer-hls-chromium-"))
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
            [
                str(public.CHROME),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}",
                f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}",
                f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,1000",
                "--autoplay-policy=no-user-gesture-required",
                "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = adaptive.connect_event_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        control = click_hls_control(client)
        print("REACTPLAYER-HLS-CONTROL:", json.dumps(control, sort_keys=True), flush=True)
        player = wait_hls_player(client)
        print("REACTPLAYER-HLS-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        renditions = observed_renditions(client)
        if not renditions:
            raise RuntimeError("no browser-observed Mux rendition manifest")
        selected_manifest = renditions[-1]
        print("REACTPLAYER-HLS-BROWSER-TRAFFIC:", json.dumps({"rendition_manifests":[redacted_url(url) for url in renditions],"selected":redacted_url(selected_manifest)}, sort_keys=True), flush=True)

        button_point = json.loads(client.evaluate("JSON.stringify((()=>{const r=document.querySelector('#dm-media-download-button')?.getBoundingClientRect();if(!r)throw Error('media button disappeared');return {x:r.left+r.width/2,y:r.top+r.height/2,top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}})())"))
        print("REACTPLAYER-HLS-DOWNLOAD-BUTTON:", json.dumps(button_point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})

        db = public.db_path(str(home))
        created = public.wait_job(db, HLS_SOURCE, timeout=90)
        assert created.get("media") is True, created
        assert created.get("source") == HLS_SOURCE, created
        destination = root / "Downloads" / "reactplayer-hls.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "reactplayer-hls.mp4", str(destination))
        reference = browser_hls_reference(client, selected_manifest, root)
        print("BROWSER-REACTPLAYER-HLS-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        assert reference["segments"] > 0, reference
        completed = public.wait_completed(db, created["id"], timeout=300)
        output_size = destination.stat().st_size
        output_hash = support.sha256(str(destination))
        assert output_size == reference["size"], (completed, output_size, reference)
        assert output_hash == reference["hash"], (completed, output_hash, reference)
        ffprobe_run = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,sample_rate", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
        ffprobe = json.loads(ffprobe_run.stdout)
        streams = ffprobe.get("streams", [])
        duration = float(ffprobe.get("format", {}).get("duration", 0.0))
        assert any(stream.get("codec_type") == "video" for stream in streams), ffprobe
        assert any(stream.get("codec_type") == "audio" for stream in streams), ffprobe
        assert 55.0 <= duration <= 65.0, ffprobe
        ownership = browser_ownership(client)
        assert ownership["context"] and not ownership["context"]["defaultPrevented"] and ownership["context"]["isTrusted"], ownership
        assert ownership["click"] and not ownership["click"]["defaultPrevented"] and ownership["click"]["isTrusted"] and ownership["click"]["ctrlKey"], ownership
        assert len(public.jobs(db)) == 1, public.jobs(db)
        print(f"BROWSER-OWNERSHIP: PASS ({json.dumps(ownership, sort_keys=True)})", flush=True)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print("REACTPLAYER-HLS-NATIVE-RESULT:", json.dumps({"state":completed.get("state"),"provisional":completed.get("provisional"),"bytes":output_size,"sha256":output_hash,"ffprobe":ffprobe}, sort_keys=True), flush=True)
        print(
            "PUBLIC-REACTPLAYER-HLS-CHROMIUM: PASS "
            f"(page={PAGE}, source={HLS_SOURCE}, selected_manifest={redacted_url(selected_manifest)}, "
            f"segments={reference['segments']}, output_bytes={output_size}, sha256={output_hash}, "
            f"jobs={len(public.jobs(db))}, browser_downloads={browser_files})",
            flush=True,
        )
        print("PUBLIC-REACTPLAYER-HLS-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
        raise
    finally:
        if client is not None:
            client.sock.close()
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
        if os.environ.get("DM_KEEP_REACTPLAYER_HLS") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-REACTPLAYER-HLS-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
