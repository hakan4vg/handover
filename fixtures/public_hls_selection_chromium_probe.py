#!/usr/bin/env python3
"""Real Chromium hls.js stream-selector initiation proof.

The existing public HLS proof auto-loaded one fixed stream through a query
parameter. This probe starts at the official hls.js demo, selects the same
finite VOD through the page's live stream selector, and then exercises the
real extension -> native messaging -> Add Download -> commit path. Reusing the
known source keeps the independent browser reference byte-exact while the
initiation path is genuinely different.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

import public_hls_chromium_probe as base

BIN = base.BIN
EXTENSION = base.EXTENSION
HLS_SOURCE = base.HLS_SOURCE
PAGE = "https://hlsjs.video-dev.org/demo/"


def wait_page(client, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
            if last.get("href", "").split("#", 1)[0].split("?", 1)[0] == PAGE and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public hls.js selector page did not load: {last}")


def select_stream(client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const select=document.querySelector('#streamSelect');"
            "const options=Array.from(select?.options||[]).map(option=>({value:option.value,text:option.textContent.trim()}));"
            f"const target={json.dumps(HLS_SOURCE)};"
            "const option=options.find(item=>item.value==='bigBuckBunny480p');"
            "if(!select||!option)return {ready:!!select,options,target,selected:null};"
            "select.value=option.value;"
            "select.dispatchEvent(new Event('change',{bubbles:true}));"
            "return {ready:true,options,target,selected:{value:select.value,text:option.text,target}};"
            "})())"
        )
        last = json.loads(raw)
        if (last.get("selected") or {}).get("value") == "bigBuckBunny480p":
            return last
        time.sleep(0.5)
    raise RuntimeError(f"hls.js stream selector did not expose target: {last}")


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-public-hls-selection-chromium-")
    home = os.path.join(root, "home")
    profile = os.path.join(root, "profile")
    downloads = os.path.join(profile, "Default", "Downloads")
    destination = os.path.join(root, "Downloads", "public-hls-selector.ts")
    os.makedirs(home)
    base.public.write_native_manifest(home, profile)
    inspector_port = base.support.free_port()
    chrome_port = base.support.free_port()
    xvfb, display = base.hls.start_xvfb()
    base.support.DISPLAY = display
    app_log_path = os.path.join(root, "app.log")
    app_log = open(app_log_path, "wb")
    app = subprocess.Popen([BIN], env=base.public.browser_env(home, inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    client = None
    try:
        base.support.wait_db(home)
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [
                str(base.public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=base.public.browser_env(home, inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base.public.wait_chrome(chrome_port)
        client = base.public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(base.public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        selected = select_stream(client)
        print("HLS-SELECTION:", json.dumps(selected, sort_keys=True), flush=True)
        player = base.wait_hls_player(client)
        print("HLS-SELECTION-PLAYER:", json.dumps(player, sort_keys=True), flush=True)

        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const button=document.querySelector('#dm-media-download-button');"
            "if(!button)throw Error('media button disappeared');const rect=button.getBoundingClientRect();"
            "return {x:rect.left+rect.width/2,y:rect.top+rect.height/2,top:rect.top,left:rect.left,right:rect.right,bottom:rect.bottom,width:rect.width,height:rect.height};})())"
        ))
        print("HLS-SELECTION-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})

        db = base.public.db_path(home)
        created = base.public.wait_job(db, HLS_SOURCE, timeout=90)
        assert created.get("media") is True and created.get("source") == HLS_SOURCE, created
        print("HLS-SELECTION-NATIVE-JOB:", json.dumps({"id": created["id"], "source": created["source"], "media": created.get("media"), "state": created.get("state"), "name": created.get("name")}, sort_keys=True), flush=True)
        base.public.commit_via_cli(home, inspector_port, created["id"], "public-hls-selector.ts", destination)

        reference = base.browser_hls_reference(client)
        print("BROWSER-HLS-SELECTION-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        assert reference["manifest"] == HLS_SOURCE and reference["segments"] == 64, reference
        completed = base.public.wait_completed(db, created["id"], timeout=300)
        output_size = os.path.getsize(destination)
        output_hash = base.support.sha256(destination)
        assert output_size == reference["size"], (completed, output_size, reference)
        assert output_hash == reference["hash"], (completed, output_hash, reference)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(base.public.jobs(db)) == 1, base.public.jobs(db)
        browser_files = []
        if os.path.exists(downloads):
            browser_files = [os.path.relpath(os.path.join(directory, filename), profile) for directory, _, filenames in os.walk(downloads) for filename in filenames]
        assert not browser_files, browser_files
        print("HLS-SELECTION-NATIVE-RESULT:", json.dumps({"state": completed["state"], "provisional": completed.get("provisional"), "bytes": output_size, "sha256": output_hash, "browser_downloads": browser_files}, sort_keys=True), flush=True)
        print(f"PUBLIC-HLS-SELECTION-CHROMIUM: PASS (page={PAGE}, selected_source={HLS_SOURCE}, player_src={player['src']}, segments={reference['segments']}, output_bytes={output_size}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("PUBLIC-HLS-SELECTION-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if os.path.exists(app_log_path):
            app_log.flush()
            print(f"APP-LOG: {open(app_log_path, encoding='utf-8', errors='replace').read()}", flush=True)
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
        if os.environ.get("DM_KEEP_PUBLIC_HLS_SELECTION") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-HLS-SELECTION-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
