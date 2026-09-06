#!/usr/bin/env python3
"""Real Chromium proof: bounded POST-body replay for form downloads.

SPEC §5.1 lists method/body as capturable context "when safely reproducible".
The extension observes urlencoded form bodies within the documented size and
lifetime bounds, and the native job replays that body as POST. This probe serves
a POST-only attachment (GET answers 405) and asserts byte/hash equality with the
browser's own POST download, one browser download, and one managed job.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

PAYLOAD = random.Random(0xC0FFEE).randbytes(65536)
FORM_BODY = b"fixture=post-only"
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME
DOWNLOAD_URL = ""
PAGE = ""


class PostOnlyServer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.methods: list[tuple[str, str, bytes]] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _send(self, status: int, content_type: str, body: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_GET(self):
                if self.path == "/":
                    page = b"""<!doctype html><meta charset=utf-8><title>Post-only form fixture</title>
<form id=form method=post action=/purchase><input name=fixture value=post-only><button id=submit type=submit>Buy download</button></form>"""
                    self._send(200, "text/html; charset=utf-8", page)
                    return
                if self.path == "/purchase":
                    with proxy.lock:
                        proxy.methods.append(("GET", self.path, b""))
                    self._send(405, "text/plain", b"post required")
                    return
                self._send(404, "text/plain", b"not found")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with proxy.lock:
                    proxy.methods.append(("POST", self.path, body))
                if self.path != "/purchase" or body != FORM_BODY:
                    self._send(404, "text/plain", b"not found")
                    return
                self._send(200, "application/octet-stream", PAYLOAD, {"Content-Disposition": 'attachment; filename="post-only.bin"'})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def wait_job_for_source(db: str, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in public.jobs(db) if job.get("source", "") == DOWNLOAD_URL]
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
        raw = client.evaluate("(async()=>JSON.stringify(await chrome.downloads.search({limit:50})))()")
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
        matching = [item for item in last if "post-only.bin" in (item.get("filename", "") + item.get("url", "") + item.get("finalUrl", ""))]
        complete = [item for item in matching if item.get("state") == "complete" and not item.get("error")]
        if complete:
            path = Path(complete[-1]["filename"])
            if path.exists() and path.stat().st_size > 0:
                return complete[-1], path
        time.sleep(0.5)
    raise RuntimeError(f"browser POST download did not complete: {last}")


def wait_terminal_job(db: str, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            items = [job for job in public.jobs(db) if job.get("id") == job_id]
            if items and items[-1].get("state") in ("completed", "failed"):
                return items[-1]
            if items:
                last = items[-1]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"no terminal state for job: {last}")


def main() -> int:
    global DOWNLOAD_URL, PAGE
    server = PostOnlyServer()
    DOWNLOAD_URL = server.base_url + "/purchase"
    PAGE = server.base_url + "/"
    root = Path(tempfile.mkdtemp(prefix="dm-postonly-"))
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
        chrome = subprocess.Popen(
            [str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]
            + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
               f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
               "--window-size=1280,900", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, PAGE)
        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const b=document.querySelector('#submit');const r=b?.getBoundingClientRect();"
            "return r?{x:r.left+r.width/2,y:r.top+r.height/2,type:b.type,method:b.form?.method}:null})())"
        ))
        assert point and point["type"] == "submit" and point["method"] == "post", point
        print("POSTONLY-SUBMIT:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        native_job = wait_job_for_source(db)
        print("POSTONLY-NATIVE-JOB:", json.dumps({"id": native_job["id"], "source": native_job["source"], "state": native_job["state"]}, sort_keys=True), flush=True)
        # Browser-side reference: the browser's own POST download.
        browser_record, browser_path = wait_browser_download(chrome_port, profile)
        browser_size = browser_path.stat().st_size
        browser_hash = hashlib.sha256(browser_path.read_bytes()).hexdigest()
        print("POSTONLY-BROWSER:", json.dumps({"state": browser_record.get("state"), "error": browser_record.get("error"), "bytes": browser_size, "sha256": browser_hash}, sort_keys=True), flush=True)
        managed = root / "Managed" / "post-only.bin"
        public.commit_via_cli(str(home), inspector_port, native_job["id"], "post-only.bin", str(managed))
        done = wait_terminal_job(db, native_job["id"])
        print("POSTONLY-TERMINAL:", json.dumps({"state": done.get("state"), "error": done.get("error")}, sort_keys=True), flush=True)
        assert done.get("state") == "completed", done
        assert done.get("provisional") is False, done
        native_hash = hashlib.sha256(managed.read_bytes()).hexdigest()
        assert browser_size == len(PAYLOAD), (browser_size, len(PAYLOAD))
        assert managed.stat().st_size == len(PAYLOAD) and native_hash == browser_hash, native_hash
        with server.lock:
            posts = [body for method, path, body in server.methods if method == "POST"]
        assert posts and all(body == FORM_BODY for body in posts), posts
        print(f"POSTONLY-REPLAY: PASS (output_bytes={len(PAYLOAD)}, output_sha256={native_hash}, jobs=1, browser_downloads=1)", flush=True)
        print("POSTONLY-REPLAY-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}", flush=True)
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
        server.stop()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"POSTONLY-REPLAY-PROBE: FAIL: {error}", flush=True)
        raise
