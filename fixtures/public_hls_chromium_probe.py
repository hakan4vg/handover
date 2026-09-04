#!/usr/bin/env python3
"""Real Chromium blob/MSE HLS capture proof.

The page is the hls.js project's public demo with its fixed 480p Big Buck
Bunny HLS VOD stream. The demo feeds a blob: MediaSource URL to its video
player; the extension must resolve that player to the observed HLS playlist.
The completed native output is kept as .ts so it is byte-identical to the
ordered segment concatenation computed by fetch() in the same browser page.
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
from urllib.parse import quote

import public_chromium_probe as public
import cdp_drive
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
HLS_SOURCE = "https://test-streams.mux.dev/x36xhzz/url_6/193039199_mp4_h264_aac_hq_7.m3u8"
PAGE = "https://hlsjs.video-dev.org/demo/?src=" + quote(HLS_SOURCE, safe="")


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


def wait_hls_player(client: cdp_drive.CDP, timeout: float = 75.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "(async()=>{"
                "const v=document.querySelector('video');"
                "if(v){v.scrollIntoView({block:'center'});v.muted=true;try{await v.play()}catch{}}"
                "const r=v?.getBoundingClientRect();"
                "return JSON.stringify({src:v?.currentSrc||v?.src||'',blob:(v?.currentSrc||v?.src||'').startsWith('blob:'),readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,button:!!document.querySelector('#dm-media-download-button'),rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null});"
                "})()"
            )
            last = json.loads(raw)
            if last.get("blob") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public HLS blob player did not become playable/injected: {last}")


def browser_hls_reference(client: cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const manifest={json.dumps(HLS_SOURCE)};"
        "const bytes=async url=>{const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status+' '+url);return new Uint8Array(await response.arrayBuffer())};"
        "const text=new TextDecoder();"
        "const lines=raw=>text.decode(raw).split(/\\r?\\n/).map(line=>line.trim()).filter(Boolean);"
        "const playlist=lines(await bytes(manifest));"
        "const segments=playlist.filter(line=>!line.startsWith('#')).map(line=>new URL(line,manifest).href);"
        "if(!segments.length)throw Error('HLS playlist has no segments');"
        "const parts=await Promise.all(segments.map(bytes));"
        "const joined=new Uint8Array(parts.reduce((sum,part)=>sum+part.length,0));"
        "let offset=0;for(const part of parts){joined.set(part,offset);offset+=part.length}"
        "const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',joined));"
        "return JSON.stringify({manifest,segments:segments.length,size:joined.length,hash:Array.from(digest).map(value=>value.toString(16).padStart(2,'0')).join('')});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser HLS reference failed: {result['error']}")
    return result


def browser_ownership(client: cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-hls-save-as-probe');old?.remove();"
        "const a=document.createElement('a');a.id='dm-hls-save-as-probe';"
        "a.href='https://hlsjs.video-dev.org/basic-usage.html?dm_hls_ownership=1';"
        "a.textContent='Browser-owned link';"
        "a.style.cssText='position:fixed;top:24px;left:24px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);window.__dmHlsOwnership={context:null,click:null};"
        "document.addEventListener('contextmenu',event=>{if(event.target.closest('#dm-hls-save-as-probe')){window.__dmHlsOwnership.context={defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,button:event.button};event.preventDefault();}},false);"
        "document.addEventListener('click',event=>{if(event.target.closest('#dm-hls-save-as-probe')){window.__dmHlsOwnership.click={defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,ctrlKey:event.ctrlKey,button:event.button};event.preventDefault();}},false);"
        "const rect=a.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2};})())"
    )
    point = json.loads(raw)
    for button, modifiers in (("right", 0), ("left", 2)):
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": button, "clickCount": 1, "modifiers": modifiers})
    return json.loads(client.evaluate("JSON.stringify(window.__dmHlsOwnership)"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-hls-chromium-"))
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
        client = public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        player = wait_hls_player(client)
        print("HLS-PLAYER:", json.dumps(player, sort_keys=True), flush=True)

        button_point = json.loads(client.evaluate("JSON.stringify((()=>{const r=document.querySelector('#dm-media-download-button')?.getBoundingClientRect();if(!r)throw Error('media button disappeared');return {x:r.left+r.width/2,y:r.top+r.height/2,top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}})())"))
        print("HLS-BUTTON:", json.dumps(button_point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": button_point["x"], "y": button_point["y"], "button": "left", "clickCount": 1, "modifiers": 0})

        db = public.db_path(str(home))
        created = public.wait_job(db, HLS_SOURCE, timeout=90)
        assert created.get("media") is True, created
        assert created.get("source") == HLS_SOURCE, created
        destination = root / "Downloads" / "public-hls-mse.ts"
        public.commit_via_cli(str(home), inspector_port, created["id"], "public-hls-mse.ts", str(destination))
        reference = browser_hls_reference(client)
        print("BROWSER-HLS-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        assert reference["manifest"] == HLS_SOURCE, reference
        assert reference["segments"] == 64, reference
        completed = public.wait_completed(db, created["id"], timeout=300)
        output_size = destination.stat().st_size
        output_hash = support.sha256(str(destination))
        assert output_size == reference["size"], (completed, output_size, reference)
        assert output_hash == reference["hash"], (completed, output_hash, reference)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        ownership = browser_ownership(client)
        assert ownership["context"] and not ownership["context"]["defaultPrevented"] and ownership["context"]["isTrusted"], ownership
        assert ownership["click"] and not ownership["click"]["defaultPrevented"] and ownership["click"]["isTrusted"] and ownership["click"]["ctrlKey"], ownership
        assert len(public.jobs(db)) == 1, public.jobs(db)
        print(f"BROWSER-OWNERSHIP: PASS ({json.dumps(ownership, sort_keys=True)})", flush=True)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(
            "PUBLIC-HLS-CHROMIUM: PASS "
            f"(page={PAGE}, player_src={player['src']}, manifest={HLS_SOURCE}, "
            f"segments={reference['segments']}, output_bytes={output_size}, "
            f"sha256={output_hash}, jobs={len(public.jobs(db))}, browser_downloads={browser_files})",
            flush=True,
        )
        print("PUBLIC-HLS-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_PUBLIC_HLS") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-HLS-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
