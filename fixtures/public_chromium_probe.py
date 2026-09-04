#!/usr/bin/env python3
"""Real Chromium + public-site browser integration proof.

This deliberately uses a fresh profile, a resident real binary, the built
extension, and the W3Schools public HTML video example. No site resolver is used.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = support.BIN
EXTENSION = str(Path(__file__).resolve().parents[1] / "extension" / "dist")
PAGE = "https://www.w3schools.com/html/html5_video.asp"
DEV_EXTENSION_ID = "mogdhelapdlmkfgeeaogeehnlclhcnkn"
CHROME = next(
    Path(path)
    for path in sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    if path.exists()
)

# Import the repository's stdlib-only CDP driver without executing its CLI.
_saved_argv = sys.argv[:]
sys.argv = ["cdp_drive.py", "1", "-", "/tmp/unused.png"]
import cdp_drive  # noqa: E402
sys.argv = _saved_argv


def db_path(home: str) -> str:
    return os.path.join(home, ".local", "share", "com.downloadmanager.app", "download-manager.db")


def jobs(db: str) -> list[dict]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT payload FROM jobs ORDER BY created_at").fetchall()
    return [json.loads(payload) for (payload,) in rows]


def wait_job(db: str, source: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if job.get("source") == source]
        if last:
            return last[-1]
        time.sleep(0.1)
    raise RuntimeError(f"capture did not create a job for {source!r}: {last}")


def wait_completed(db: str, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in jobs(db) if job.get("id") == job_id), None)
        if last and last.get("state") == "completed" and last.get("provisional") is False:
            return last
        time.sleep(0.1)
    raise RuntimeError(f"job did not complete: {last}")


def sha256_browser(client: cdp_drive.CDP, url: str) -> tuple[int, str]:
    options = json.dumps({"cache": "no-store"})
    raw = client.evaluate(
        "(async()=>{"
        f"const response=await fetch({json.dumps(url)},{options});"
        "if(!response.ok)return JSON.stringify({error:`HTTP ${response.status}`});"
        "const bytes=new Uint8Array(await response.arrayBuffer());"
        "const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));"
        "return JSON.stringify({size:bytes.length,hash:Array.from(digest).map(value=>value.toString(16).padStart(2,'0')).join('')});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser reference fetch failed for {url}: {result['error']}")
    return int(result["size"]), result["hash"]


def commit_via_cli(home: str, inspector_port: int, job_id: str, name: str, destination: str) -> None:
    payload = {"id": job_id, "input": {"name": name, "destination": destination}}
    result = subprocess.run(
        [BIN, "--commit", json.dumps(payload, separators=(",", ":"))],
        env=browser_env(home, inspector_port),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"--commit forwarding failed: exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}")
    print(f"COMMIT-CLI: forwarded job={job_id} exit={result.returncode}", flush=True)


def wait_chrome(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                if json.load(response).get("webSocketDebuggerUrl"):
                    return
        except Exception as error:
            last = error
        time.sleep(0.2)
    raise RuntimeError(f"Chromium CDP did not become ready: {last}")


def connect_chrome(port: int) -> cdp_drive.CDP:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    page = next((item for item in targets if item.get("type") == "page"), None)
    if page is None:
        raise RuntimeError(f"Chromium has no page target: {targets}")
    ws_url = page["webSocketDebuggerUrl"]
    path = ws_url.split(f"127.0.0.1:{port}", 1)[1]
    client = cdp_drive.CDP(cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    client.call("Page.enable")
    client.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1400, "deviceScaleFactor": 1, "mobile": False})
    return client


def extension_diagnostic(port: int, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    targets: list[dict] = []
    worker = None
    while time.time() < deadline:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
            targets = json.load(response)
        worker = next((item for item in targets if item.get("type") == "service_worker" and item.get("url", "").endswith("/background.js")), None)
        if worker is not None:
            break
        time.sleep(0.2)
    if worker is None:
        return {"error": "background service worker target missing", "workers": [item.get("url") for item in targets if item.get("type") == "service_worker"]}
    path = worker["webSocketDebuggerUrl"].split(f"127.0.0.1:{port}", 1)[1]
    client = cdp_drive.CDP(cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    try:
        result = client.evaluate(
            "(async()=>{"
            "const c=typeof chrome==='undefined'?null:chrome;"
            "const base={extensionId:c?.runtime?.id??null,storageType:typeof c?.storage,runtimeType:typeof c?.runtime};"
            "if(base.storageType!=='object'||base.runtimeType!=='object')return JSON.stringify(base);"
            "let storage;try{storage=await c.storage.local.get('dm-policy')}catch(error){storage={error:String(error)}}"
            "let native=await new Promise(resolve=>{try{c.runtime.sendNativeMessage('com.downloadmanager.host',{type:'get-policy'},response=>resolve({response:response??null,lastError:c.runtime.lastError?.message??null}))}catch(error){resolve({response:null,lastError:String(error)})}});"
            "return JSON.stringify({...base,storage,native});})()"
        )
        return {"worker": worker.get("url"), **(json.loads(result) if isinstance(result, str) else {"raw": result})}
    finally:
        client.sock.close()


def wait_page(client: cdp_drive.CDP, url: str, timeout: float = 45.0) -> None:
    client.call("Page.navigate", {"url": url})
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate("JSON.stringify({href: location.href, state: document.readyState})")
            last = json.loads(raw)
            if last.get("href") == url and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public page did not load: {last}")


def wait_public_player(client: cdp_drive.CDP, timeout: float = 35.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=Array.from(document.querySelectorAll('video,audio'));"
                "const visible=v=>{const r=v.getBoundingClientRect();return r.width>=120&&r.height>=40&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth};"
                "const v=media.find(visible)||media[0];"
                "if(v){const before=v.getBoundingClientRect();const target=Math.max(0,before.top+scrollY-innerHeight/2);window.scrollTo(0,target);document.scrollingElement.scrollTop=target;document.documentElement.scrollTop=target;document.body.scrollTop=target;v.muted=true;void v.play();}"
                "const r=v?.getBoundingClientRect();"
                "return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,button:!!document.querySelector('#dm-media-download-button'),media:media.length,viewport:{width:innerWidth,height:innerHeight,scrollY,scrollingElement:document.scrollingElement?.tagName,scrollTop:document.scrollingElement?.scrollTop,scrollHeight:document.scrollingElement?.scrollHeight},rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null};})())"
            )
            last = json.loads(raw)
            if last.get("src") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    document = client.evaluate("JSON.stringify({title:document.title,body:document.body?.innerText?.slice(0,500),videos:document.querySelectorAll('video').length,iframes:Array.from(document.querySelectorAll('iframe')).map(frame=>frame.src).slice(0,10),html:document.documentElement.outerHTML.slice(-3000)})")
    raise RuntimeError(f"public player did not become playable/injected: {last}; document={document}")


def write_native_manifest(home: str, profile: str) -> None:
    app_root = Path(home) / ".local" / "share" / "com.downloadmanager.app"
    app_root.mkdir(parents=True, exist_ok=True)
    wrapper = app_root / "public-probe-native-host.sh"
    wrapper.write_text(f"#!/bin/sh\nexec {BIN!r} --native-host\n")
    wrapper.chmod(0o755)
    manifest = {
        "name": "com.downloadmanager.host",
        "description": "Download Manager public Chromium probe",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [
            "chrome-extension://mfdaoipoffnpeijnjminkdhecpnoemel/",
            f"chrome-extension://{DEV_EXTENSION_ID}/",
        ],
    }
    contents = json.dumps(manifest, indent=2)
    for config in (
        Path(home) / ".config" / "google-chrome-for-testing" / "NativeMessagingHosts",
        Path(profile) / "NativeMessagingHosts",
    ):
        config.mkdir(parents=True, exist_ok=True)
        (config / "com.downloadmanager.host.json").write_text(contents)


def browser_env(home: str, inspector_port: int) -> dict[str, str]:
    env = support.app_env(home, inspector_port)
    env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
    env["DM_EXTENSION_ID"] = DEV_EXTENSION_ID
    return env


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-public-chromium-")
    home = str(Path(root) / "home")
    profile = str(Path(root) / "profile")
    downloads = Path(profile) / "Default" / "Downloads"
    Path(home).mkdir(parents=True)
    write_native_manifest(home, profile)
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    time.sleep(1)
    db = db_path(home)
    app_log_path = Path(root) / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=browser_env(home, inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    browser_client = None
    try:
        support.wait_db(home)
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        app_client = None
        chrome = subprocess.Popen(
            [
                str(CHROME),
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
                "--window-size=1280,900",
                "--autoplay-policy=no-user-gesture-required",
                "about:blank",
            ],
            env=browser_env(home, inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_chrome(chrome_port)
        with urllib.request.urlopen(f"http://127.0.0.1:{chrome_port}/json/list", timeout=10) as response:
            print("CHROME-TARGETS:", json.dumps([(item.get("type"), item.get("url")) for item in json.load(response)]), flush=True)
        browser_client = connect_chrome(chrome_port)
        wait_page(browser_client, PAGE)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        print(
            "PLAYER-DIAGNOSTIC:",
            browser_client.evaluate(
                "JSON.stringify(Array.from(document.querySelectorAll('video,audio')).map((v,i)=>({i,src:v.currentSrc||v.src||'',readyState:v.readyState,paused:v.paused,ended:v.ended,rect:(()=>{const r=v.getBoundingClientRect();return {top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}})()})))"
            ),
            flush=True,
        )
        player = wait_public_player(browser_client)
        media_source = player["src"]
        media_click = browser_client.evaluate("(document.querySelector('#dm-media-download-button')?.click(), 'clicked')")
        assert media_click == "clicked", media_click
        media_job = wait_job(db, media_source)
        media_name = "public-w3-media" + (Path(urlsplit(media_source).path).suffix or ".media")
        media_dest = str(Path(root) / "Downloads" / media_name)
        commit_via_cli(home, inspector_port, media_job["id"], media_name, media_dest)
        media_done = wait_completed(db, media_job["id"])
        media_size, media_hash = sha256_browser(browser_client, media_source)
        assert Path(media_dest).stat().st_size == media_size, (media_done, media_size)
        assert support.sha256(media_dest) == media_hash, (media_done, media_hash)

        anchor_result = browser_client.evaluate(
            "JSON.stringify((()=>{const a=document.createElement('a'); a.href=new URL('/html/mov_bbb.mp4?dm_public_anchor=1',location.href).href; a.setAttribute('download','public-w3-link.mp4'); a.textContent='Download public video'; a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647'; document.body.appendChild(a); "
            "const event=new MouseEvent('click',{bubbles:true,cancelable:true,button:0}); "
            "const dispatched=a.dispatchEvent(event); "
            "return {dispatched,defaultPrevented:event.defaultPrevented,href:a.href};})())"
        )
        anchor = json.loads(anchor_result)
        assert anchor.get("error") is None, anchor
        assert anchor["defaultPrevented"] is True and anchor["dispatched"] is False, anchor
        anchor_source = anchor["href"]
        anchor_job = wait_job(db, anchor_source)
        anchor_dest = str(Path(root) / "Downloads" / "public-w3-link.mp4")
        commit_via_cli(home, inspector_port, anchor_job["id"], "public-w3-link.mp4", anchor_dest)
        anchor_done = wait_completed(db, anchor_job["id"])
        anchor_size, anchor_hash = sha256_browser(browser_client, anchor_source)
        assert Path(anchor_dest).stat().st_size == anchor_size, (anchor_done, anchor_size)
        assert support.sha256(anchor_dest) == anchor_hash, (anchor_done, anchor_hash)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(
            "PUBLIC-CHROMIUM: PASS "
            f"(page={PAGE}, media_source={media_source}, media_bytes={media_size}, "
            f"anchor_source={anchor_source}, anchor_bytes={anchor_size}, "
            f"jobs={len(jobs(db))}, browser_downloads={browser_files})",
            flush=True,
        )
        print("PUBLIC-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    finally:
        if browser_client is not None:
            try:
                browser_client.sock.close()
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
        if os.environ.get("DM_KEEP_PUBLIC_PROBE") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
