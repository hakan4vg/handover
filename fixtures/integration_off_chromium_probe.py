#!/usr/bin/env python3
"""Real Chromium proof that integration-off leaves browser downloads alone."""
from __future__ import annotations

import hashlib
import json
import os
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

BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME
FILENAME = "integration-off.bin"
PAYLOAD = bytes((index * 31 + 11) % 256 for index in range(48 * 1024))


class DownloadServer:
    def __init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/integration-off.bin":
                    self.send_error(404)
                    return
                with owner.lock:
                    owner.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

        self.lock = threading.Lock()
        self.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def worker_client(port: int) -> public.cdp_drive.CDP:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    worker = next(
        item
        for item in targets
        if item.get("type") == "service_worker" and item.get("url", "").endswith("/background.js")
    )
    path = worker["webSocketDebuggerUrl"].split(f"127.0.0.1:{port}", 1)[1]
    client = public.cdp_drive.CDP(public.cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    return client


def set_interception_off(client: public.cdp_drive.CDP) -> dict:
    extension_page = f"chrome-extension://{public.DEV_EXTENSION_ID}/popup.html"
    client.call("Page.navigate", {"url": extension_page})
    deadline = time.time() + 30
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
            if last.get("href") == extension_page and last.get("state") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError(f"extension page did not load: {last}")
    raw = client.evaluate(
        "(async()=>await chrome.runtime.sendMessage({type:'update-policy',patch:{interceptDownloads:false}}))()"
    )
    response = raw if isinstance(raw, dict) else json.loads(raw)
    if response.get("policy", {}).get("interceptDownloads") is not False:
        raise RuntimeError(f"extension page did not disable interception: {response}")
    return response


def browser_downloads(port: int) -> list[dict]:
    client = worker_client(port)
    try:
        raw = client.evaluate("(async()=>JSON.stringify(await chrome.downloads.search({limit:50})))()")
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    finally:
        client.sock.close()


def wait_browser_download(port: int, source: str, timeout: float = 60.0) -> tuple[dict, Path]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = browser_downloads(port)
        except Exception:
            last = []
        matching = [
            item
            for item in last
            if item.get("url") == source
            or item.get("finalUrl") == source
            or item.get("filename", "").endswith(FILENAME)
        ]
        complete = [item for item in matching if item.get("state") == "complete" and not item.get("error")]
        if len(complete) == 1:
            path = Path(complete[0]["filename"])
            if path.exists() and path.stat().st_size == len(PAYLOAD):
                return complete[0], path
        time.sleep(0.2)
    raise RuntimeError(f"integration-off browser download did not complete exactly once: {last}")


def click_link(client: public.cdp_drive.CDP, source: str) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-integration-off-link');old?.remove();"
        f"const a=document.createElement('a');a.id='dm-integration-off-link';a.href={json.dumps(source)};"
        f"a.download={json.dumps(FILENAME)};a.textContent='Browser-owned download';"
        "a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);const r=a.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,href:a.href,download:a.download};})())"
    )
    point = json.loads(raw)
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0},
    )
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0},
    )
    return point


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-integration-off-chromium-"))
    home = root / "home"
    profile = root / "profile"
    home.mkdir(parents=True)
    server = DownloadServer()
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    chrome = None
    client = None
    source = f"http://127.0.0.1:{server.port}/integration-off.bin"
    page = f"http://127.0.0.1:{server.port}/page"
    try:
        chrome = subprocess.Popen(
            [
                str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,900", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        policy = set_interception_off(client)
        print("POLICY-OFF:", json.dumps(policy, sort_keys=True), flush=True)
        public.wait_page(client, page)
        point = click_link(client, source)
        print("TRUSTED-BROWSER-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        record, path = wait_browser_download(chrome_port, source)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        expected = hashlib.sha256(PAYLOAD).hexdigest()
        print(
            "BROWSER-OWNED-DOWNLOAD:",
            json.dumps(
                {
                    "id": record.get("id"),
                    "state": record.get("state"),
                    "error": record.get("error"),
                    "byExtensionId": record.get("byExtensionId"),
                    "url": record.get("url"),
                    "finalUrl": record.get("finalUrl"),
                    "filename": record.get("filename"),
                    "bytesReceived": record.get("bytesReceived"),
                    "size": len(data),
                    "sha256": digest,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        time.sleep(1)
        records = browser_downloads(chrome_port)
        matching = [item for item in records if item.get("url") == source or item.get("finalUrl") == source or item.get("filename", "").endswith(FILENAME)]
        app_db = home / ".local" / "share" / "com.downloadmanager.app" / "download-manager.db"
        native_processes = []
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if cmdline and cmdline[0] == str(BIN).encode():
                native_processes.append(str(entry))
        assert policy["policy"]["interceptDownloads"] is False, policy
        assert len(matching) == 1, matching
        assert record.get("state") == "complete" and not record.get("error"), record
        assert not record.get("byExtensionId"), record
        assert len(data) == len(PAYLOAD) and digest == expected, (len(data), digest, expected)
        assert server.requests == 1, server.requests
        assert not app_db.exists(), app_db
        assert not native_processes, native_processes
        print(
            f"INTEGRATION-OFF: PASS (browser_bytes={len(data)}, sha256={digest}, downloads={len(matching)}, "
            f"source_requests={server.requests}, browser_initiator=page, native_jobs=0, native_processes=0)",
            flush=True,
        )
        print("INTEGRATION-OFF-CHROMIUM-PROBE: PASS", flush=True)
        return 0
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
        server.stop()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_INTEGRATION_OFF") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"INTEGRATION-OFF-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
