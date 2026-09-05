#!/usr/bin/env python3
"""Real Chromium proof: a real GitHub release asset download through the real page.

Navigates to the flask 3.1.3 releases page and trusted-clicks the actual
"Source code (zip)" asset link. The plain link (no `download` attribute) goes
through the browser's ordinary download; the observe-only fallback must capture
the same source with the header-resolved filename (flask-3.1.3.zip) and the
resident must commit a byte/hash-identical copy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_chromium_probe as public
import hls_fmp4_probe as hls
import segmented_restart_probe as support

RELEASE_PAGE = "https://github.com/pallets/flask/releases/tag/3.1.3"
ASSET_HREF = "https://github.com/pallets/flask/archive/refs/tags/3.1.3.zip"
EXPECTED_NAME = "flask-3.1.3.zip"
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME


def db_path(home: str) -> str:
    return public.db_path(home)


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_job_for_source(db: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if "flask" in job.get("source", "") or "codeload.github.com" in job.get("source", "")]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"release fallback did not create a native job: {last}")


def worker_downloads(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    worker = next((item for item in targets if item.get("type") == "service_worker" and item.get("url", "").endswith("/background.js")), None)
    if worker is None:
        return []
    path = worker["webSocketDebuggerUrl"].split(f"127.0.0.1:{port}", 1)[1]
    client = public.cdp_drive.CDP(public.cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    try:
        raw = client.evaluate("(async()=>JSON.stringify(await chrome.downloads.search({limit:50})))()")
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    finally:
        client.sock.close()


def wait_browser_download(port: int, profile: Path, timeout: float = 120.0) -> tuple[dict, Path]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = worker_downloads(port)
        except Exception:
            last = []
        matching = [item for item in last if EXPECTED_NAME in (item.get("filename", "") + item.get("finalUrl", "") + item.get("url", ""))]
        complete = [item for item in matching if item.get("state") == "complete" and not item.get("error")]
        if complete:
            path = Path(complete[-1]["filename"])
            if path.exists() and path.stat().st_size > 0:
                return complete[-1], path
        time.sleep(0.5)
    raise RuntimeError(f"browser release download did not complete: {last}")


def find_asset_link(client: public.cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const all=Array.from(document.querySelectorAll('a'));"
        "const a=all.find(item=>{const h=(item.getAttribute('href')||'')+' '+item.href;return h.includes('/archive/refs/tags/') && (item.textContent||'').toLowerCase().includes('source code');});"
        "if(!a)return {error:'asset link missing',count:all.filter(item=>(item.getAttribute('href')||'').includes('/archive/refs/tags/')).length};"
        "const r=a.getBoundingClientRect();a.scrollIntoView({block:'center'});const r2=a.getBoundingClientRect();"
        "return {href:a.href,text:a.textContent.trim(),x:r2.left+r2.width/2,y:r2.top+r2.height/2};})())"
    )
    point = json.loads(raw)
    if point.get("error"):
        raise RuntimeError(point["error"])
    return point


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-github-release-"))
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
            [str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]
            + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,900", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": RELEASE_PAGE})
        deadline = time.time() + 60
        loaded = None
        while time.time() < deadline:
            try:
                loaded = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
                if loaded.get("href", "").startswith("https://github.com/pallets/flask/releases") and loaded.get("state") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        print("RELEASE-PAGE:", json.dumps(loaded, sort_keys=True), flush=True)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        asset = find_asset_link(client)
        print("RELEASE-ASSET:", json.dumps(asset, sort_keys=True), flush=True)
        assert asset["href"] == ASSET_HREF, asset
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": asset["x"], "y": asset["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": asset["x"], "y": asset["y"], "button": "left", "clickCount": 1, "modifiers": 0})

        db = db_path(str(home))
        native_job = wait_job_for_source(db)
        print("RELEASE-NATIVE-JOB:", json.dumps({"id": native_job["id"], "source": native_job["source"], "state": native_job["state"], "name": native_job["name"]}, sort_keys=True), flush=True)
        browser_record, browser_path = wait_browser_download(chrome_port, profile)
        browser_name = Path(browser_record["filename"]).name
        assert native_job["name"] == EXPECTED_NAME, {"native_name": native_job["name"], "expected": EXPECTED_NAME, "browser_record": browser_record}
        assert browser_name == EXPECTED_NAME, {"browser_name": browser_name, "browser_record": browser_record}
        browser_size = browser_path.stat().st_size
        browser_hash = support.sha256(str(browser_path))
        print("BROWSER-DOWNLOAD:", json.dumps({"id": browser_record.get("id"), "state": browser_record.get("state"), "error": browser_record.get("error"), "filename": browser_record.get("filename"), "size": browser_size, "sha256": browser_hash}, sort_keys=True), flush=True)
        native_destination = root / "Managed" / EXPECTED_NAME
        public.commit_via_cli(str(home), inspector_port, native_job["id"], EXPECTED_NAME, str(native_destination))
        native_done = public.wait_completed(db, native_job["id"], timeout=180)
        native_size = native_destination.stat().st_size
        native_hash = support.sha256(str(native_destination))
        print("NATIVE-DOWNLOAD:", json.dumps({"state": native_done["state"], "provisional": native_done.get("provisional"), "size": native_size, "sha256": native_hash}, sort_keys=True), flush=True)
        assert native_size == browser_size and native_hash == browser_hash, (native_done, browser_record)
        assert native_done["state"] == "completed" and native_done.get("provisional") is False, native_done
        assert len(jobs(db)) == 1, jobs(db)
        print(f"GITHUB-RELEASE: PASS (page={RELEASE_PAGE}, name={EXPECTED_NAME}, browser_bytes={browser_size}, sha256={browser_hash}, native_bytes={native_size}, native_sha256={native_hash}, jobs={len(jobs(db))})", flush=True)
        print("GITHUB-RELEASE-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
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
        if os.environ.get("DM_KEEP_GITHUB_RELEASE") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"GITHUB-RELEASE-PROBE: FAIL: {error}", flush=True)
        raise
