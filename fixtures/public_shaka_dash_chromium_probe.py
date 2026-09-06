#!/usr/bin/env python3
"""Real Chromium + Shaka DASH SegmentBase capture proof.

The official Shaka demo's finite Angel One asset uses an MSE-backed video and
an ISO-BMFF DASH MPD whose representations use SegmentBase byte ranges. The
probe drives the real Angel One card and Shaka player, the injected media
button, native messaging, and resident completion. It records the browser's
manifest/range traffic and later compares the resident mux against an
independent reconstruction of the selected SegmentBase tracks.
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
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import public_chromium_probe as public
import cdp_drive
import adaptive_dash_quality_probe as adaptive
import segmented_restart_probe as support
import hls_fmp4_probe as hls

BIN = public.BIN
EXTENSION = public.EXTENSION
MPD_SOURCE = "https://storage.googleapis.com/shaka-demo-assets/angel-one/dash.mpd"
PAGE = "https://shaka-project.github.io/shaka-player/demo/"


def redacted_url(url: str) -> str:
    parts = urlsplit(url)
    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    query = "&".join(f"{key}=[REDACTED]" for key in keys)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


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


def load_shaka_asset(client: cdp_drive.CDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    raw = None
    card_point = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const card=[...document.querySelectorAll('.asset-card')].find(item=>item.querySelector('.mdl-card__title-text')?.textContent.trim()==='Angel One');"
            "const button=card?.querySelector('.mdl-card__actions button');"
            "if(!button)return null;const r=button.getBoundingClientRect();"
            "return {text:button.textContent.trim(),x:r.left+r.width/2,y:r.top+r.height/2,width:r.width,height:r.height};"
            "})())"
        )
        card_point = json.loads(raw) if isinstance(raw, str) else raw
        if card_point and card_point.get("width", 0) > 0 and card_point.get("height", 0) > 0:
            break
        time.sleep(0.5)
    if not card_point:
        raise RuntimeError(f"Shaka Angel One Play card missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": card_point["x"], "y": card_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": card_point["x"], "y": card_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})

    last = None
    control_point = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate(
                "JSON.stringify((()=>{"
                "const v=document.querySelector('#video');const r=v?.getBoundingClientRect();"
                "const c=document.querySelector('.shaka-play-button');const cr=c?.getBoundingClientRect();"
                "return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,ended:v?.ended??false,duration:v?.duration??0,currentTime:v?.currentTime??0,button:!!document.querySelector('#dm-media-download-button'),videoRect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null,control:c?{aria:c.getAttribute('aria-label'),rect:cr?{top:cr.top,left:cr.left,right:cr.right,bottom:cr.bottom,width:cr.width,height:cr.height}:null}:null};"
                "})())"
            ))
            if last.get("readyState", 0) >= 2 and last.get("duration", 0) > 0 and last.get("button"):
                if last.get("paused") or last.get("ended"):
                    control = last.get("control") or {}
                    rect = control.get("rect") or {}
                    if rect.get("width", 0) > 0 and rect.get("height", 0) > 0:
                        control_point = {"x":(rect["left"]+rect["right"])/2,"y":(rect["top"]+rect["bottom"])/2,"aria":control.get("aria")}
                        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": control_point["x"], "y": control_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
                        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": control_point["x"], "y": control_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
                        time.sleep(0.5)
                        continue
                if not last.get("paused") and not last.get("ended"):
                    return {**last, "card_point":card_point, "control_point":control_point}
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Shaka Angel One player did not become playable/injected: {last}")


def shaka_active_tracks(client: cdp_drive.CDP) -> list[dict]:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const video=document.querySelector('#video');"
        "const player=video?.ui?.getControls?.().getPlayer?.();"
        "const tracks=player?.getVariantTracks?.()||[];"
        "return tracks.filter(track=>track.active).map(track=>({id:track.id,videoId:track.videoId,audioId:track.audioId,originalVideoId:track.originalVideoId,originalAudioId:track.originalAudioId,audioLanguage:track.audioLanguage,videoLanguage:track.videoLanguage,width:track.width,height:track.height,bandwidth:track.bandwidth,videoMimeType:track.videoMimeType,audioMimeType:track.audioMimeType,videoCodec:track.videoCodec,audioCodec:track.audioCodec}));"
        "})())"
    )
    tracks = json.loads(raw)
    if not tracks or not any(track.get("videoId") is not None for track in tracks) or not any(track.get("audioId") is not None for track in tracks):
        raise RuntimeError(f"Shaka did not expose one active audio/video variant: {tracks}")
    return tracks


def browser_dash_reference(client: cdp_drive.CDP, root: Path, active_tracks: list[dict], observed_segments: list[str]) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const manifest={json.dumps(MPD_SOURCE)};"
        "const response=await fetch(manifest,{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status+' '+manifest);"
        "const xml=new DOMParser().parseFromString(await response.text(),'application/xml');"
        "if(xml.querySelector('parsererror'))throw Error('invalid MPD XML');"
        "const ns='*';const base=new URL('./',manifest).href;"
        f"const activeTracks={json.dumps(active_tracks)};"
        f"const observed={json.dumps(observed_segments)};"
        "const resources=new Set(observed.map(url=>String(url).split('?')[0]));"
        "const children=(node,name)=>Array.from(node.children).filter(child=>child.localName===name);"
        "const text=(node,name)=>children(node,name)[0]?.textContent?.trim()||'';"
        "const sets=Array.from(xml.getElementsByTagNameNS(ns,'AdaptationSet')).filter(set=>['audio','video'].includes((set.getAttribute('contentType')||set.getAttribute('mimeType')||'').split('/')[0]));"
        "const tracks=sets.flatMap(set=>{const kind=(set.getAttribute('contentType')||set.getAttribute('mimeType')||'').split('/')[0];const ids=activeTracks.flatMap(track=>{const id=kind==='video'?(track.originalVideoId??track.videoId):kind==='audio'?(track.originalAudioId??track.audioId):null;return id===null||id===undefined?[]:[String(id)]});const reps=children(set,'Representation');const candidates=reps.map(rep=>({rep,url:new URL(text(rep,'BaseURL')||text(set,'BaseURL'),base).href}));const chosen=candidates.find(item=>ids.includes(String(item.rep.getAttribute('id'))));if(!chosen||!resources.has(chosen.url.split('?')[0]))return [];const sb=children(chosen.rep,'SegmentBase')[0]||children(set,'SegmentBase')[0];return [{kind,representation:chosen.rep.getAttribute('id')||'',url:chosen.url,indexRange:sb?.getAttribute('indexRange')||'',initializationRange:children(sb,'Initialization')[0]?.getAttribute('range')||'',browserObserved:true}];});"
        "if(!tracks.some(track=>track.kind==='video')||!tracks.some(track=>track.kind==='audio'))throw Error('no browser-observed audio/video SegmentBase tracks; resources='+JSON.stringify([...resources].filter(url=>/storage.googleapis.com|shaka-project.github.io/.test(url)).slice(-80)));"
        "return JSON.stringify({manifest,tracks});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser DASH reference failed: {result['error']}")
    paths = []
    for index, track in enumerate(result["tracks"]):
        path = root / f"shaka-reference-{index}-{track['kind']}.mp4"
        with urllib.request.urlopen(track["url"], timeout=120) as response, path.open("wb") as output:
            shutil.copyfileobj(response, output)
        track["bytes"] = path.stat().st_size
        track["sha256"] = support.sha256(str(path))
        paths.append(path)
    ordered = [path for track, path in zip(result["tracks"], paths) if track["kind"] in {"video", "audio"}]
    if len(ordered) < 2:
        raise RuntimeError(f"reference did not resolve separate audio/video tracks: {result}")
    reference_path = root / "shaka-reference-mux.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ordered[0]), "-i", str(ordered[1]), "-map", "0:0", "-map", "1:0", "-c", "copy", str(reference_path)], check=True)
    result["output_bytes"] = reference_path.stat().st_size
    result["output_sha256"] = support.sha256(str(reference_path))
    return result


def observed_media_urls(client: adaptive.EventCDP) -> list[str]:
    urls = []
    seen = set()
    for event in client.take_events():
        if event.get("method") != "Network.requestWillBeSent":
            continue
        request = event.get("params", {}).get("request", {})
        url = request.get("url", "")
        parsed = urlsplit(url)
        if parsed.netloc.lower() != "storage.googleapis.com" or not parsed.path.lower().endswith((".mp4", ".webm")):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


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


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-dash-chromium-"))
    print("DASH-ROOT:", root, flush=True)
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
        client = adaptive.connect_event_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        player = load_shaka_asset(client)
        active_tracks = shaka_active_tracks(client)
        print("DASH-PLAYER:", json.dumps({**player, "active_tracks": active_tracks}, sort_keys=True), flush=True)
        button_point = json.loads(client.evaluate("JSON.stringify((()=>{const r=document.querySelector('#dm-media-download-button')?.getBoundingClientRect();if(!r)throw Error('media button disappeared');return {x:r.left+r.width/2,y:r.top+r.height/2,top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}})())"))
        print("DASH-BUTTON:", json.dumps(button_point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        created = public.wait_job(db, MPD_SOURCE, timeout=90)
        assert created.get("media") is True and created.get("source") == MPD_SOURCE, created
        destination = root / "Downloads" / "public-dash-mse.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "public-dash-mse.mp4", str(destination))
        browser_observed_segments = observed_media_urls(client)
        managed_selected_segments = created.get("selectedSegments", [])
        assert managed_selected_segments, created
        if browser_observed_segments:
            observed_paths = {url.split("?", 1)[0] for url in browser_observed_segments}
            assert all(url.split("?", 1)[0] in observed_paths for url in managed_selected_segments), {"managed": managed_selected_segments, "browser_observed": browser_observed_segments}
        print("SHAKA-BROWSER-OBSERVED:", json.dumps([redacted_url(url) for url in browser_observed_segments], sort_keys=True), flush=True)
        print("SHAKA-MANAGED-SELECTED:", json.dumps([redacted_url(url) for url in managed_selected_segments], sort_keys=True), flush=True)
        reference = browser_dash_reference(client, root, active_tracks, browser_observed_segments)
        active_paths = {track["url"].split("?", 1)[0] for track in reference["tracks"]}
        managed_paths = {url.split("?", 1)[0] for url in managed_selected_segments}
        assert active_paths <= managed_paths, {"active": sorted(active_paths), "managed": managed_selected_segments}
        print("BROWSER-DASH-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        assert reference["manifest"] == MPD_SOURCE and len(reference["tracks"]) == 2, reference
        completed = public.wait_completed(db, created["id"], timeout=180)
        output_size = destination.stat().st_size
        output_hash = support.sha256(str(destination))
        assert output_hash == reference["output_sha256"], {"native_sha256": output_hash, "reference_sha256": reference["output_sha256"]}
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
        ffprobe = json.loads(probe.stdout)
        stream_types = sorted(stream["codec_type"] for stream in ffprobe.get("streams", []))
        assert stream_types == ["audio", "video"], ffprobe
        assert 59.0 <= float(ffprobe["format"]["duration"]) <= 61.0, ffprobe
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
