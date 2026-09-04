#!/usr/bin/env python3
"""Real Chromium adaptive HLS representation-selection proof."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import public_chromium_probe as public
import public_hls_chromium_probe as public_hls
import cdp_drive
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://hlsjs.video-dev.org/demo"
MASTER = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"


def wait_page(client: cdp_drive.CDP, timeout: float = 45.0) -> None:
    client.call("Page.navigate", {"url": PAGE})
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
            if last.get("href", "").rstrip("/") == PAGE and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"adaptive HLS page did not load: {last}")


def wait_player(client: cdp_drive.CDP, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const v=document.querySelector('video');"
                "if(v){v.scrollIntoView({block:'center'});v.muted=true;void v.play();}"
                "const r=v?.getBoundingClientRect();"
                "return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??null,button:!!document.querySelector('#dm-media-download-button'),rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null};})())"
            )
            last = json.loads(raw)
            if last.get("src", "").startswith("blob:") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"adaptive HLS player did not become ready: {last}")


def force_representation(client: cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        "const h=window.hls;"
        "if(!h)return JSON.stringify({error:'window.hls missing'});"
        "const levels=h.levels.map((l,i)=>({index:i,url:Array.isArray(l.url)?l.url[0]:l.url,bitrate:l.bitrate,width:l.width,height:l.height}));"
        "if(levels.length<2)return JSON.stringify({error:'fewer than two levels',levels});"
        "const current=h.currentLevel;"
        "const target=current===1?Math.min(2,levels.length-1):1;"
        "h.currentLevel=target;"
        "return JSON.stringify({current,target,levels,selected:levels[target]});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(result)
    return result


def wait_representation(client: cdp_drive.CDP, target: dict, timeout: float = 45.0) -> dict:
    marker = target["url"]
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate(
                "JSON.stringify({"
                "current:window.hls?.currentLevel??null,"
                "selected:(()=>{const u=(window.hls?.levels||[])[window.hls?.currentLevel]?.url;return Array.isArray(u)?u[0]:(u||null)})(),"
                "playing:!(document.querySelector('video')?.paused??true),"
                "resources:performance.getEntriesByType('resource').map(e=>e.name).filter(n=>/\\.(m3u8|ts)(?:[?#]|$)/i.test(n)).slice(-20)"
                "})"
            ))
            if last.get("current") == target["index"] and last.get("selected") == marker and any(marker.rsplit("/", 1)[0] in item for item in last.get("resources", [])):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"selected HLS representation was not observed: target={target}, last={last}")


def browser_variant_reference(client: cdp_drive.CDP, manifest: str) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const manifest={json.dumps(manifest)};"
        "const get=async u=>{const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status+' '+u);return new Uint8Array(await r.arrayBuffer())};"
        "const raw=new TextDecoder().decode(await get(manifest));"
        "const segments=raw.split(/\\r?\\n/).map(x=>x.trim()).filter(x=>x&&!x.startsWith('#')).map(x=>new URL(x,manifest).href);"
        "const parts=await Promise.all(segments.map(get));"
        "const joined=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));let offset=0;for(const p of parts){joined.set(p,offset);offset+=p.length}"
        "const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',joined));"
        "return JSON.stringify({manifest,segments:segments.length,size:joined.length,hash:Array.from(digest).map(x=>x.toString(16).padStart(2,'0')).join('')});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(result)
    return result


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-adaptive-hls-quality-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    chrome = None
    browser_client = None
    app = None
    app_log = None
    try:
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        app_log_path = root / "app.log"
        app_log = app_log_path.open("wb")
        app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
        support.wait_db(str(home))
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
        browser_client = public.connect_chrome(chrome_port)
        wait_page(browser_client)
        player = wait_player(browser_client)
        print("ADAPTIVE-HLS-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        changed = force_representation(browser_client)
        print("ADAPTIVE-HLS-LEVEL-CHANGE:", json.dumps(changed, sort_keys=True), flush=True)
        observed = wait_representation(browser_client, changed["selected"])
        print("ADAPTIVE-HLS-OBSERVED:", json.dumps(observed, sort_keys=True), flush=True)

        button = json.loads(browser_client.evaluate("JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');const r=b?.getBoundingClientRect();return r?{x:r.left+r.width/2,y:r.top+r.height/2}:null})())"))
        if not button:
            raise RuntimeError("media button disappeared after level change")
        browser_client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": button["x"], "y": button["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        browser_client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": button["x"], "y": button["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        try:
            job = public.wait_job(db, changed["selected"]["url"], timeout=90)
        except Exception:
            print("ADAPTIVE-HLS-JOBS-AFTER-CAPTURE:", json.dumps(public.jobs(db), sort_keys=True), flush=True)
            raise
        print("ADAPTIVE-HLS-JOB:", json.dumps({"id":job["id"],"source":job["source"],"state":job["state"]}, sort_keys=True), flush=True)
        destination = str(root / "Downloads" / "adaptive-selected.ts")
        public.commit_via_cli(str(home), inspector_port, job["id"], "adaptive-selected.ts", destination)
        reference = browser_variant_reference(browser_client, changed["selected"]["url"])
        print("BROWSER-ADAPTIVE-HLS-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        completed = public.wait_completed(db, job["id"], timeout=180)
        output_size = Path(destination).stat().st_size
        output_hash = support.sha256(destination)
        assert output_size == reference["size"], (completed, output_size, reference)
        assert output_hash == reference["hash"], (completed, output_hash, reference)
        ownership = public_hls.browser_ownership(browser_client)
        assert ownership["context"] and not ownership["context"]["defaultPrevented"] and ownership["context"]["isTrusted"], ownership
        assert ownership["click"] and not ownership["click"]["defaultPrevented"] and ownership["click"]["isTrusted"] and ownership["click"]["ctrlKey"], ownership
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print("BROWSER-OWNERSHIP: PASS", json.dumps(ownership, sort_keys=True), flush=True)
        print(f"ADAPTIVE-HLS-QUALITY: PASS (target={json.dumps(changed['selected'], sort_keys=True)}, output_bytes={output_size}, sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("ADAPTIVE-HLS-QUALITY-PROBE: PASS", flush=True)
        return 0
    finally:
        if browser_client is not None:
            browser_client.sock.close()
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill(); chrome.wait(timeout=10)
        if app is not None and app.poll() is None:
            app.terminate()
            try: app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill(); app.wait(timeout=10)
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill(); xvfb.wait(timeout=5)
        if app_log is not None:
            app_log.close()
        if os.environ.get("DM_KEEP_ADAPTIVE_HLS") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ADAPTIVE-HLS-QUALITY-PROBE: FAIL: {error}", flush=True)
        raise
