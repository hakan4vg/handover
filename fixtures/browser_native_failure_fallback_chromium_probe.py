#!/usr/bin/env python3
"""Real Chromium proof of ordinary-download browser fallback after native failure.

A disposable stdio native-messaging host is registered and deliberately returns
``ok:false`` for ``capture-acquisition``. A trusted click on an explicit
``<a download>`` must therefore be restored through ``chrome.downloads`` without
recursively entering ordinary interception. The probe uses a small local HTTP
fixture so the browser artifact can be compared byte-for-byte without contacting
an unsafe public source.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import struct
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
DEV_EXTENSION_ID = public.DEV_EXTENSION_ID
FILENAME = "browser-fallback.bin"
PAYLOAD = bytes((index * 29 + 7) % 256 for index in range(64 * 1024)
)


class DownloadServer:
    def __init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/browser-fallback.bin":
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


def browser_downloads(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    worker = next(
        (
            item
            for item in targets
            if item.get("type") == "service_worker"
            and item.get("url", "").endswith("/background.js")
        ),
        None,
    )
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


def wait_browser_download(port: int, profile: Path, source: str, timeout: float = 60.0) -> tuple[dict, Path]:
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
    raise RuntimeError(f"browser fallback did not complete exactly one download: {last}")


def click_download_link(client: public.cdp_drive.CDP, source: str) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-browser-fallback-link');old?.remove();"
        f"const a=document.createElement('a');a.id='dm-browser-fallback-link';a.href={json.dumps(source)};"
        f"a.download={json.dumps(FILENAME)};a.textContent='Download fallback fixture';"
        "a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);const r=a.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,href:a.href,download:a.download};})())"
    )
    point = json.loads(raw)
    if point.get("download") != FILENAME:
        raise RuntimeError(f"fallback link was not an explicit download: {point}")
    client.call(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": point["x"],
            "y": point["y"],
            "button": "left",
            "clickCount": 1,
            "modifiers": 0,
        },
    )
    client.call(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": point["x"],
            "y": point["y"],
            "button": "left",
            "clickCount": 1,
            "modifiers": 0,
        },
    )
    return point


def write_failure_manifest(home: Path, profile: Path, root: Path) -> Path:
    app_root = home / ".local" / "share" / "com.downloadmanager.app"
    app_root.mkdir(parents=True, exist_ok=True)
    host = root / "failure-native-host.py"
    log_path = root / "failure-native-messages.jsonl"
    host.write_text(
        "#!/usr/bin/env python3\n"
        "import json, struct, sys\n"
        f"LOG = {str(log_path)!r}\n"
        "while True:\n"
        "    header = sys.stdin.buffer.read(4)\n"
        "    if len(header) != 4:\n"
        "        break\n"
        "    size = struct.unpack('<I', header)[0]\n"
        "    payload = json.loads(sys.stdin.buffer.read(size))\n"
        "    details = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}\n"
        "    with open(LOG, 'a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps({'type': payload.get('type'), 'source': details.get('source'), 'name': details.get('name'), 'pageUrl': details.get('pageUrl')}) + '\\n')\n"
        "    if payload.get('type') == 'get-policy':\n"
        "        response = {'ok': True, 'policy': {'interceptDownloads': True, 'showMediaButtons': True, 'excludedSites': []}}\n"
        "    elif payload.get('type') == 'capture-acquisition':\n"
        "        response = {'ok': False, 'error': 'simulated native capture failure'}\n"
        "    else:\n"
        "        response = {'ok': True}\n"
        "    encoded = json.dumps(response, separators=(',', ':')).encode()\n"
        "    sys.stdout.buffer.write(struct.pack('<I', len(encoded)) + encoded)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    host.chmod(0o755)
    manifest = {
        "name": "com.downloadmanager.host",
        "description": "Download Manager native-failure fallback probe",
        "path": str(host),
        "type": "stdio",
        "allowed_origins": [
            "chrome-extension://mfdaoipoffnpeijnjminkdhecpnoemel/",
            f"chrome-extension://{DEV_EXTENSION_ID}/",
        ],
    }
    contents = json.dumps(manifest, indent=2)
    for config in (
        home / ".config" / "google-chrome-for-testing" / "NativeMessagingHosts",
        profile / "NativeMessagingHosts",
    ):
        config.mkdir(parents=True, exist_ok=True)
        (config / "com.downloadmanager.host.json").write_text(contents)
    return log_path


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-browser-native-failure-fallback-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    failure_log = write_failure_manifest(home, profile, root)
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    server = DownloadServer()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    chrome = None
    client = None
    source = f"http://127.0.0.1:{server.port}/browser-fallback.bin"
    page = f"http://127.0.0.1:{server.port}/page"
    try:
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
                page,
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        deadline = time.time() + 30
        last = None
        while time.time() < deadline:
            try:
                last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
                if last.get("href") == page and last.get("state") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError(f"fallback page did not load: {last}")

        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
        native = diagnostic.get("native") if isinstance(diagnostic, dict) else None
        native_response = native.get("response") if isinstance(native, dict) else None
        if not isinstance(native, dict) or native.get("lastError") or not isinstance(native_response, dict) or native_response.get("ok") is not True:
            raise RuntimeError(f"fake native host did not answer policy request: {diagnostic}")
        point = click_download_link(client, source)
        print("TRUSTED-FALLBACK-CLICK:", json.dumps(point, sort_keys=True), flush=True)

        browser_record, browser_path = wait_browser_download(chrome_port, profile, source)
        messages = [json.loads(line) for line in failure_log.read_text(encoding="utf-8").splitlines() if line.strip()] if failure_log.exists() else []
        print("FAKE-NATIVE-MESSAGES:", json.dumps(messages, sort_keys=True), flush=True)
        captures = [message for message in messages if message.get("type") == "capture-acquisition"]
        assert len(captures) == 1, messages
        browser_bytes = browser_path.read_bytes()
        browser_hash = hashlib.sha256(browser_bytes).hexdigest()
        expected_hash = hashlib.sha256(PAYLOAD).hexdigest()
        print(
            "BROWSER-NATIVE-FAILURE-FALLBACK-DOWNLOAD:",
            json.dumps(
                {
                    "id": browser_record.get("id"),
                    "state": browser_record.get("state"),
                    "error": browser_record.get("error"),
                    "url": browser_record.get("url"),
                    "finalUrl": browser_record.get("finalUrl"),
                    "filename": browser_record.get("filename"),
                    "bytesReceived": browser_record.get("bytesReceived"),
                    "size": len(browser_bytes),
                    "sha256": browser_hash,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        time.sleep(1)
        records = browser_downloads(chrome_port)
        matching = [item for item in records if item.get("url") == source or item.get("finalUrl") == source or item.get("filename", "").endswith(FILENAME)]
        app_db = home / ".local" / "share" / "com.downloadmanager.app" / "download-manager.db"
        product_processes = [
            entry
            for entry in Path("/proc").glob("[0-9]*")
            if (entry / "cmdline").exists()
            and (entry / "cmdline").read_bytes().split(b"\0")
            and (entry / "cmdline").read_bytes().split(b"\0")[0] == str(BIN).encode()
        ]
        assert len(matching) == 1, matching
        assert browser_record.get("state") == "complete" and not browser_record.get("error"), browser_record
        assert len(browser_bytes) == len(PAYLOAD)
        assert browser_hash == expected_hash, (browser_hash, expected_hash)
        assert server.requests == 1, server.requests
        assert not app_db.exists(), app_db
        assert not product_processes, [str(entry) for entry in product_processes]
        print(
            f"BROWSER-NATIVE-FAILURE-FALLBACK: PASS (browser_bytes={len(browser_bytes)}, sha256={browser_hash}, "
            f"downloads={len(matching)}, source_requests={server.requests}, native_jobs=0, "
            f"native_processes=0, browser_filename={browser_record.get('filename')!r})",
            flush=True,
        )
        print("BROWSER-NATIVE-FAILURE-FALLBACK-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_BROWSER_FALLBACK") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"BROWSER-NATIVE-FAILURE-FALLBACK-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
