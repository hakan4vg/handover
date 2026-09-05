#!/usr/bin/env python3
"""Real Chromium proof of GitHub's JS download button (a blob download).

GitHub's real blob page exposes a data-testid=download-raw-button control whose
handler fetches the raw file and initiates a client-side `blob:` download. A
clean profile records the browser-owned artifact (bytes + SHA-256). A fresh
resident+extension profile trusted-clicks the same button and proves the
extension's ordinary-download path, which is HTTP-only by contract, leaves the
blob download browser-owned: one complete browser download, zero native jobs.
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
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

PAGE = "https://github.com/pallets/flask/blob/main/README.md"
EXPECTED_SOURCE = "https://raw.githubusercontent.com/pallets/flask/refs/heads/main/README.md"
EXPECTED_NAME = "README.md"
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_json(client, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def wait_page(client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = evaluate_json(client, "{href:location.href,state:document.readyState,button:!!document.querySelector('[data-testid=\\\"download-raw-button\\\"]')}")
            if last.get("state") == "complete" and last.get("button"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"GitHub blob page did not load: {last}")


def button(client) -> dict:
    point = evaluate_json(client, """(()=>{const b=document.querySelector('[data-testid="download-raw-button"]'); if(!b)return {error:'download raw button missing'}; b.scrollIntoView({block:'center'}); const r=b.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2,test:b.getAttribute('data-testid'),aria:b.getAttribute('aria-label'),outer:b.outerHTML.slice(0,600)};})()""")
    if point.get("error"):
        raise RuntimeError(point["error"])
    return point


def click(client, point: dict) -> None:
    for event, btn, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call("Input.dispatchMouseEvent", {"type":event,"x":point["x"],"y":point["y"],"button":btn,"buttons":buttons,"clickCount":1,"modifiers":0})


def wait_file(directory: Path, timeout: float = 90.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = [p for p in directory.glob("*") if p.is_file() and not p.name.endswith(".crdownload") and p.stat().st_size > 0]
        if candidates:
            return candidates[0]
        time.sleep(0.4)
    raise RuntimeError(f"browser raw download did not complete: {sorted(str(p) for p in directory.glob('*'))}")


def browser_reference(root: Path) -> dict:
    profile = root / "reference-profile"
    output = root / "reference-downloads"
    output.mkdir(parents=True)
    port = support.free_port()
    chrome = subprocess.Popen([str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--window-size=1280,1200", PAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None
    try:
        public.wait_chrome(port)
        client = public.connect_chrome(port)
        client.call("Browser.setDownloadBehavior", {"behavior":"allow","downloadPath":str(output),"eventsEnabled":True})
        loaded = wait_page(client)
        point = button(client)
        click(client, point)
        path = wait_file(output)
        result = {"page":PAGE,"loaded":loaded,"button":point,"filename":path.name,"size":path.stat().st_size,"sha256":sha256(path)}
        print("BROWSER-REFERENCE:", json.dumps(result, sort_keys=True), flush=True)
        return result
    finally:
        if client is not None:
            try: client.sock.close()
            except Exception: pass
        if chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill(); chrome.wait(timeout=10)


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


def native_run(root: Path, reference: dict) -> None:
    home = root / "home"
    profile = root / "native-profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "managed" / EXPECTED_NAME
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
        chrome = subprocess.Popen([str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"] + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,1200", PAGE], env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        public.wait_chrome(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        client = public.connect_chrome(chrome_port)
        loaded = wait_page(client)
        point = button(client)
        click(client, point)
        print("TRUSTED-RAW-BUTTON-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        deadline = time.time() + 30
        observed = []
        while time.time() < deadline:
            try:
                observed = worker_downloads(chrome_port)
            except Exception:
                observed = []
            if observed:
                break
            time.sleep(0.5)
        print("BROWSER-DOWNLOADS-IN-EXTENSION-PROFILE:", json.dumps(observed, sort_keys=True), flush=True)
        blob_items = [item for item in observed if (item.get("url") or "").startswith("blob:") and item.get("state") == "complete" and not item.get("error")]
        assert len(blob_items) == 1, observed
        assert Path(blob_items[-1]["filename"]).name == EXPECTED_NAME, observed
        browser_size = blob_items[-1].get("fileSize") or blob_items[-1].get("totalBytes")
        assert browser_size == reference["size"], (observed, reference)
        db = public.db_path(str(home))
        deadline = time.time() + 15
        current = public.jobs(db)
        while time.time() < deadline:
            current = public.jobs(db)
            if current:
                break
            time.sleep(0.3)
        assert not current, current
        print(f"GITHUB-RAW-BUTTON: PASS (page={PAGE}, source=blob:https://github.com/…, filename={EXPECTED_NAME}, browser_bytes={browser_size}, browser_sha256={reference['sha256']}, native_jobs={len(current)}, browser_downloads={len(blob_items)})", flush=True)
        print("GITHUB-RAW-BUTTON-PROBE: PASS", flush=True)
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


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-github-raw-button-"))
    try:
        reference = browser_reference(root)
        native_run(root, reference)
        return 0
    finally:
        if os.environ.get("DM_KEEP_GITHUB_RAW_BUTTON") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"GITHUB-RAW-BUTTON-PROBE: FAIL: {error}", flush=True)
        raise
