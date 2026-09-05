#!/usr/bin/env python3
"""Real Chromium proof that an explicit interaction selects the sibling audio player.

The Able Player external5 demo exposes both audio and video after a trusted
"Show players" click. This probe then uses the audio player's own Play control,
verifies the product button is anchored to that audio player, and captures the
selected Ogg source rather than the larger video.
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
from urllib.parse import urlsplit, urlunsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_ableplayer_hidden_chromium_probe as base

PAGE = "https://ableplayer.github.io/ableplayer/demos/external5.html"
AUDIO_SOURCE_SUFFIX = "/ableplayer/media/smallf.ogg"


def redacted_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_json(client, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def describe_players(client) -> dict:
    return evaluate_json(
        client,
        "Array.from(document.querySelectorAll('audio,video')).map(item=>{"
        "const r=item.getBoundingClientRect();const source=item.currentSrc||item.src||item.querySelector('source[src]')?.src||'';"
        "return {tag:item.tagName.toLowerCase(),id:item.id,source,readyState:item.readyState,paused:item.paused,ended:item.ended,controls:item.controls,"
        "rect:{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height},"
        "wrapper:(()=>{const w=item.closest('div.able');if(!w)return null;const x=w.getBoundingClientRect();return {top:x.top,left:x.left,right:x.right,bottom:x.bottom,width:x.width,height:x.height}})()};})",
    )


def wait_players(client, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = describe_players(client)
        audio = next((item for item in last if item["tag"] == "audio" and item["source"].endswith(AUDIO_SOURCE_SUFFIX)), None)
        video = next((item for item in last if item["tag"] == "video"), None)
        if audio and video and audio["readyState"] >= 2 and video["readyState"] >= 2 and audio["wrapper"]:
            return {"players": last, "audio": audio, "video": video}
        time.sleep(0.5)
    raise RuntimeError(f"Able Player audio/video did not become available: {last}")


def audio_control(client) -> dict:
    return evaluate_json(
        client,
        "(()=>{const audio=[...document.querySelectorAll('audio')].find(item=>(item.currentSrc||item.src||item.querySelector('source[src]')?.src||'').endsWith(%s));"
        "if(!audio)return {error:'audio missing'};const root=audio.closest('div.able')||audio.parentElement;"
        "const controls=[...root.querySelectorAll('button,[role=button],input')];"
        "const play=controls.find(item=>/play/i.test(item.getAttribute('aria-label')||'')||/play/i.test(item.getAttribute('title')||'')||/^play$/i.test(item.textContent?.trim()||''));"
        "const target=play||audio;const r=target.getBoundingClientRect();const ar=audio.getBoundingClientRect();"
        "return {selector:play?(play.id?'#'+play.id:play.tagName.toLowerCase()):'audio',text:target.textContent?.trim()||'',aria:target.getAttribute('aria-label'),"
        "x:r.left+r.width/2,y:r.top+r.height/2,audioRect:{top:ar.top,left:ar.left,right:ar.right,bottom:ar.bottom,width:ar.width,height:ar.height},"
        "wrapperRect:(()=>{const x=root.getBoundingClientRect();return {top:x.top,left:x.left,right:x.right,bottom:x.bottom,width:x.width,height:x.height}})()};})()"
        % json.dumps(AUDIO_SOURCE_SUFFIX),
    )


def click(client, point: dict) -> None:
    for event, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call("Input.dispatchMouseEvent", {"type": event, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1, "modifiers": 0})


def wait_audio_playing(client, timeout: float = 35.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = evaluate_json(
            client,
            "(()=>{const audio=[...document.querySelectorAll('audio')].find(item=>(item.currentSrc||item.src||item.querySelector('source[src]')?.src||'').endsWith(%s));"
            "const b=document.querySelector('#dm-media-download-button');const ar=audio?.getBoundingClientRect();const br=b?.getBoundingClientRect();"
            "return {source:audio?.currentSrc||audio?.src||audio?.querySelector('source[src]')?.src||'',readyState:audio?.readyState||0,paused:audio?.paused??true,"
            "duration:audio?.duration||0,button:!!b,buttonRect:br?{top:br.top,left:br.left,right:br.right,bottom:br.bottom,width:br.width,height:br.height}:null,"
            "audioRect:ar?{top:ar.top,left:ar.left,right:ar.right,bottom:ar.bottom,width:ar.width,height:ar.height}:null,"
            "wrapperRect:(()=>{const x=audio?.closest('div.able')?.getBoundingClientRect();return x?{top:x.top,left:x.left,right:x.right,bottom:x.bottom,width:x.width,height:x.height}:null})(),"
            "trusted:window.__dmLastTrustedClick||null};})()"
            % json.dumps(AUDIO_SOURCE_SUFFIX),
        )
        last = last
        if last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"audio did not become playing with button: {last}")


def browser_reference(client, source: str) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const r=await fetch({json.dumps(source)},{{cache:'no-store'}});"
        "if(!r.ok)return JSON.stringify({error:'HTTP '+r.status});"
        "const b=new Uint8Array(await r.arrayBuffer());const d=new Uint8Array(await crypto.subtle.digest('SHA-256',b));"
        "return JSON.stringify({size:b.length,hash:Array.from(d).map(x=>x.toString(16).padStart(2,'0')).join('')});})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser reference fetch failed: {result}")
    return result


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-ableplayer-audio-selection-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    base.public.write_native_manifest(str(home), str(profile))
    inspector_port = base.support.free_port()
    chrome_port = base.support.free_port()
    xvfb, display = base.hls.start_xvfb()
    base.support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([base.BIN], env=base.public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    client = None
    try:
        base.support.wait_db(str(home))
        chrome = subprocess.Popen(
            [
                str(base.public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={base.EXTENSION}", f"--disable-extensions-except={base.EXTENSION}",
                "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=base.public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base.public.wait_chrome(chrome_port)
        client = base.adaptive.connect_event_chrome(chrome_port)
        base.public.wait_page(client, PAGE)
        client.evaluate("window.__dmLastTrustedClick=null;document.addEventListener('click',e=>{if(e.isTrusted)window.__dmLastTrustedClick={trusted:true,target:e.target?.id||e.target?.tagName||''}},true)")
        revealed = base.reveal_players(client)
        print("ABLEPLAYER-AUDIO-REVEAL:", json.dumps(revealed, sort_keys=True), flush=True)
        players = wait_players(client)
        print("ABLEPLAYER-AUDIO-VIDEO:", json.dumps(players, sort_keys=True), flush=True)
        control = audio_control(client)
        if control.get("error"):
            raise RuntimeError(control["error"])
        print("ABLEPLAYER-AUDIO-CONTROL:", json.dumps(control, sort_keys=True), flush=True)
        click(client, control)
        playing = wait_audio_playing(client)
        print("ABLEPLAYER-AUDIO-PLAYING:", json.dumps(playing, sort_keys=True), flush=True)
        button = evaluate_json(client, "(()=>{const b=document.querySelector('#dm-media-download-button');const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,text:b.textContent,aria:b.getAttribute('aria-label')}})()")
        print("ABLEPLAYER-AUDIO-BUTTON:", json.dumps(button, sort_keys=True), flush=True)
        click(client, button)
        db = base.public.db_path(str(home))
        job = base.wait_media_job(db)
        if not job.get("source", "").endswith(AUDIO_SOURCE_SUFFIX):
            raise RuntimeError(f"native job selected wrong player: {job}")
        print("ABLEPLAYER-AUDIO-NATIVE-JOB:", json.dumps({"id":job["id"],"source":redacted_url(job["source"]),"media":job.get("media"),"state":job.get("state"),"name":job.get("name")}, sort_keys=True), flush=True)
        media_name = f"ableplayer-selected-audio{Path(urlsplit(job['source']).path).suffix}"
        managed = managed_dir / media_name
        base.public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        done = base.public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        client.call("Page.navigate", {"url": "https://ableplayer.github.io/"})
        time.sleep(2)
        reference = browser_reference(client, job["source"])
        print("BROWSER-AUDIO-REFERENCE:", json.dumps({"url":redacted_url(job["source"]),**reference}, sort_keys=True), flush=True)
        if native_size != int(reference["size"]) or native_hash != reference["hash"]:
            raise RuntimeError(f"native output differs from browser audio: native={native_size}/{native_hash} browser={reference}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(base.jobs(db)) == 1, base.jobs(db)
        assert not browser_files, browser_files
        assert playing["trusted"] and playing["trusted"]["trusted"] is True, playing
        print(f"ABLEPLAYER-AUDIO-SELECTION: PASS (page={PAGE}, source={redacted_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files}, trusted_audio_click=true)", flush=True)
        print("ABLEPLAYER-AUDIO-SELECTION-CHROMIUM-PROBE: PASS", flush=True)
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
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ABLEPLAYER-AUDIO-SELECTION-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
