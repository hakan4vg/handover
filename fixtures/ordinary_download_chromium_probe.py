#!/usr/bin/env python3
"""Real Chromium proof for a plain-link ordinary download fallback."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

DOWNLOAD_URL = "https://github.com/octocat/Hello-World/archive/refs/heads/master.zip"
PAGE = "https://www.w3schools.com/html/html5_video.asp"
DEV_EXTENSION_ID = public.DEV_EXTENSION_ID
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME


def db_path(home: str) -> str:
    return public.db_path(home)


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_job_for_source(db: str, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if "github.com" in job.get("source", "") or "codeload.github.com" in job.get("source", "")]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"ordinary fallback did not create a native job: {last}")


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
        raw = client.evaluate(
            "(async()=>JSON.stringify(await chrome.downloads.search({limit:50})))()"
        )
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    finally:
        client.sock.close()


def wait_browser_download(port: int, profile: Path, timeout: float = 90.0) -> tuple[dict, Path]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = worker_downloads(port)
        except Exception:
            last = []
        matching = [item for item in last if "Hello-World" in (item.get("filename", "") + item.get("url", "") + item.get("finalUrl", ""))]
        complete = [item for item in matching if item.get("state") == "complete" and not item.get("error")]
        if complete:
            path = Path(complete[-1]["filename"])
            if path.exists() and path.stat().st_size > 0:
                return complete[-1], path
        time.sleep(0.5)
    raise RuntimeError(f"browser ordinary download did not complete: {last}")


def click_plain_link(client: public.cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-ordinary-plain-link');old?.remove();"
        f"const a=document.createElement('a');a.id='dm-ordinary-plain-link';a.href={json.dumps(DOWNLOAD_URL)};"
        "a.textContent='Download plain archive';a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);const r=a.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,download:a.getAttribute('download'),href:a.href};})())"
    )
    point = json.loads(raw)
    if point.get("download") is not None:
        raise RuntimeError(f"probe link unexpectedly has download attribute: {point}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    return point


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-ordinary-download-"))
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
        public.wait_page(client, PAGE)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        point = click_plain_link(client)
        print("PLAIN-LINK:", json.dumps(point, sort_keys=True), flush=True)
        db = db_path(str(home))
        native_job = wait_job_for_source(db)
        print("ORDINARY-FALLBACK-JOB:", json.dumps({"id": native_job["id"], "source": native_job["source"], "state": native_job["state"], "name": native_job["name"]}, sort_keys=True), flush=True)
        browser_record, browser_path = wait_browser_download(chrome_port, profile)
        browser_name = Path(browser_record["filename"]).name
        assert native_job["name"] == browser_name, {"native_name": native_job["name"], "browser_name": browser_name, "browser_record": browser_record}
        browser_size = browser_path.stat().st_size
        browser_hash = support.sha256(str(browser_path))
        print("BROWSER-DOWNLOAD:", json.dumps({"id": browser_record.get("id"), "state": browser_record.get("state"), "error": browser_record.get("error"), "url": browser_record.get("url"), "finalUrl": browser_record.get("finalUrl"), "filename": browser_record.get("filename"), "bytesReceived": browser_record.get("bytesReceived"), "size": browser_size, "sha256": browser_hash}, sort_keys=True), flush=True)
        native_destination = root / "Managed" / "ordinary-fallback.zip"
        public.commit_via_cli(str(home), inspector_port, native_job["id"], "ordinary-fallback.zip", str(native_destination))
        native_done = public.wait_completed(db, native_job["id"], timeout=120)
        native_size = native_destination.stat().st_size
        native_hash = support.sha256(str(native_destination))
        print("NATIVE-DOWNLOAD:", json.dumps({"state": native_done["state"], "provisional": native_done.get("provisional"), "size": native_size, "sha256": native_hash, "source": native_done["source"]}, sort_keys=True), flush=True)
        assert native_size == browser_size and native_hash == browser_hash, (native_done, browser_record)
        assert native_done["state"] == "completed" and native_done.get("provisional") is False, native_done
        assert browser_record.get("state") == "complete" and not browser_record.get("error"), browser_record
        assert len(jobs(db)) == 1, jobs(db)
        print(f"ORDINARY-DOWNLOAD: PASS (browser_bytes={browser_size}, sha256={browser_hash}, native_bytes={native_size}, native_sha256={native_hash}, browser_state={browser_record.get('state')}, jobs={len(jobs(db))})", flush=True)
        print("ORDINARY-DOWNLOAD-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_ORDINARY_DOWNLOAD") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ORDINARY-DOWNLOAD-PROBE: FAIL: {error}", flush=True)
        raise
