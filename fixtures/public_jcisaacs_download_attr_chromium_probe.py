#!/usr/bin/env python3
"""Real Chromium/native proof for a public same-origin <a download> page.

The jcisaacs HTML5 Download Attribute page contains a real anchor with
href=samp/htmldoc.html and download="sample-file.html". A clean Chromium
profile first records the browser-owned download bytes/hash. A second fresh
profile runs the resident binary and extension, trusted-clicks that real
anchor, and proves one native job owns the identical bytes while Chromium's
Downloads directory stays empty.
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

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

PAGE = "https://jcisaacs.com/TEST/HTML5-Test/HTML5%20Download%20Attribute%20Demo.html"
EXPECTED_SOURCE = "https://jcisaacs.com/TEST/HTML5-Test/samp/htmldoc.html"
EXPECTED_NAME = "sample-file.html"
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


def wait_page(client, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = evaluate_json(client, "{href:location.href,state:document.readyState,anchor:!!document.querySelector('a[download=\\\"sample-file.html\\\"]')}")
            if last.get("state") == "complete" and last.get("anchor"):
                return last
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"download-attribute page did not load: {last}")


def anchor(client) -> dict:
    point = evaluate_json(
        client,
        """(()=>{const a=document.querySelector('a[download="sample-file.html"]'); if(!a)return {error:'download anchor missing'}; a.scrollIntoView({block:'center'}); const r=a.getBoundingClientRect(); return {href:a.href,rawHref:a.getAttribute('href'),download:a.getAttribute('download'),text:a.textContent.trim(),x:r.left+r.width/2,y:r.top+r.height/2};})()""",
    )
    if point.get("error"):
        raise RuntimeError(point["error"])
    if point["href"] != EXPECTED_SOURCE or point["download"] != EXPECTED_NAME:
        raise AssertionError(point)
    return point


def trusted_click(client, point: dict) -> None:
    for event, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call("Input.dispatchMouseEvent", {"type":event,"x":point["x"],"y":point["y"],"button":button,"buttons":buttons,"clickCount":1,"modifiers":0})


def wait_file(path: Path, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0 and not path.with_suffix(path.suffix + ".crdownload").exists():
            return
        time.sleep(0.3)
    raise RuntimeError(f"browser download did not complete: {path}")


def browser_reference(root: Path) -> tuple[dict, Path]:
    profile = root / "reference-profile"
    output = root / "reference-downloads"
    output.mkdir(parents=True)
    port = support.free_port()
    chrome = subprocess.Popen([str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--window-size=1280,900", PAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None
    try:
        public.wait_chrome(port)
        client = public.connect_chrome(port)
        client.call("Browser.setDownloadBehavior", {"behavior":"allow","downloadPath":str(output),"eventsEnabled":True})
        loaded = wait_page(client)
        point = anchor(client)
        trusted_click(client, point)
        path = output / EXPECTED_NAME
        wait_file(path)
        result = {"page":PAGE,"loaded":loaded,"anchor":point,"filename":path.name,"size":path.stat().st_size,"sha256":sha256(path)}
        print("BROWSER-REFERENCE:", json.dumps(result, sort_keys=True), flush=True)
        return result, path
    finally:
        if client is not None:
            try: client.sock.close()
            except Exception: pass
        if chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill(); chrome.wait(timeout=10)


def native_run(root: Path, reference: dict) -> None:
    home = root / "home"
    profile = root / "native-profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "managed" / EXPECTED_NAME
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        chrome = subprocess.Popen([str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"] + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,900", PAGE], env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        public.wait_chrome(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        client = public.connect_chrome(chrome_port)
        loaded = wait_page(client)
        point = anchor(client)
        trusted_click(client, point)
        print("TRUSTED-DOWNLOAD-ATTRIBUTE-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        db = public.db_path(str(home))
        deadline = time.time() + 60
        job = None
        while time.time() < deadline:
            candidates = [item for item in public.jobs(db) if item.get("source") == EXPECTED_SOURCE]
            if candidates:
                job = candidates[-1]
                break
            time.sleep(0.2)
        if job is None:
            raise RuntimeError(f"native job missing for explicit download anchor: {public.jobs(db)}")
        assert job.get("media") is False, job
        assert job.get("name") == EXPECTED_NAME, job
        print("NATIVE-JOB:", json.dumps({"id":job["id"],"source":job["source"],"name":job["name"],"state":job["state"],"provisional":job.get("provisional"),"media":job.get("media")}, sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], EXPECTED_NAME, str(managed))
        done = public.wait_completed(db, job["id"], timeout=180)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        print("NATIVE-DOWNLOAD:", json.dumps({"state":done["state"],"provisional":done.get("provisional"),"size":native_size,"sha256":native_hash}, sort_keys=True), flush=True)
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert native_size == reference["size"] and native_hash == reference["sha256"], (done, reference)
        assert len(public.jobs(db)) == 1, public.jobs(db)
        assert not browser_files, browser_files
        print(f"JCISAACS-DOWNLOAD-ATTR: PASS (page={PAGE}, source={EXPECTED_SOURCE}, filename={EXPECTED_NAME}, browser_bytes={reference['size']}, browser_sha256={reference['sha256']}, native_bytes={native_size}, native_sha256={native_hash}, jobs={len(public.jobs(db))}, browser_downloads={browser_files})", flush=True)
        print("JCISAACS-DOWNLOAD-ATTR-PROBE: PASS", flush=True)
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


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-jcisaacs-download-"))
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    try:
        reference, _ = browser_reference(root)
        native_run(root, reference)
        return 0
    finally:
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_JCISAACS") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"JCISAACS-DOWNLOAD-ATTR-PROBE: FAIL: {error}", flush=True)
        raise
