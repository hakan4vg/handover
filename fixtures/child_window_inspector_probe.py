#!/usr/bin/env python3
"""Bounded probe for a captured Add Download child WebView target."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
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
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
FILENAME = "child-window.bin"
PAYLOAD = bytes((index * 41 + 23) % 256 for index in range(64 * 1024))


class DownloadServer:
    def __init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != f"/{FILENAME}":
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


def ws_client(port: int, path: str):
    client = support.WebKitClient.__new__(support.WebKitClient)
    setattr(client, "base64", base64)
    setattr(client, "struct", struct)
    client.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    key = base64.b64encode(b"0123456789abcdef").decode()
    client.sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    header = b""
    while b"\r\n\r\n" not in header:
        header += client.sock.recv(4096)
    if b"101" not in header.split(b"\r\n", 1)[0]:
        client.sock.close()
        raise RuntimeError(f"inspector upgrade failed for {path}: {header!r}")
    client.next_id = 0
    client.target_id = None

    def recv_tolerant() -> str:
        first, second = client._recv_exact(2)
        length = second & 0x7F
        if length == 126:
            length = client.struct.unpack(">H", client._recv_exact(2))[0]
        elif length == 127:
            length = client.struct.unpack(">Q", client._recv_exact(8))[0]
        if second & 0x80:
            client._recv_exact(4)
        return client._recv_exact(length).decode(errors="replace")

    client._recv = recv_tolerant
    client._outer("Target.setPauseOnStart", {"pauseOnStart": False})
    if client.target_id is None:
        try:
            targets = client._outer("Target.getTargets")
            for info in targets.get("targetInfos", []):
                if info.get("type") == "page":
                    client.target_id = info.get("targetId")
                    break
        except RuntimeError as error:
            raise RuntimeError(f"no target event and Target.getTargets={error}") from error
    if not client.target_id:
        raise RuntimeError("no WebKit page target")
    client._nested("Runtime.enable")
    return client


def inspector_http(port: int, path: str) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.read().decode(errors="replace")
    except Exception as error:
        return f"ERROR: {error}"


def window_count(display: str) -> int:
    result = subprocess.run(["xwininfo", "-root", "-tree"], env=dict(os.environ, DISPLAY=display), capture_output=True, text=True, check=True)
    return sum(1 for line in result.stdout.splitlines() if '"Add Download"' in line)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-child-inspector-"))
    home = root / "home"
    home.mkdir(parents=True)
    server = DownloadServer()
    xvfb = None
    app = None
    main_client = None
    forward = None
    app_log = None
    child_clients = []
    source = f"http://127.0.0.1:{server.port}/{FILENAME}"
    inspector_port = support.free_port()
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        app_log = (root / "app.log").open("wb")
        app = subprocess.Popen([BIN], env=support.app_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
        db = support.wait_db(str(home))
        main_client = support.wait_inspector(inspector_port)
        support.wait_tauri(main_client)
        raw = json.dumps({"type": "capture-acquisition", "payload": {"source": source, "name": FILENAME}}, separators=(",", ":"))
        forward = subprocess.Popen([BIN, "--capture", raw], env=support.app_env(str(home)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert forward.wait(timeout=20) == 0
        deadline = time.time() + 30
        while time.time() < deadline and window_count(display) < 1:
            time.sleep(0.2)
        windows = window_count(display)
        print(f"CHILD-WINDOW-OBSERVED: windows={windows}", flush=True)
        assert windows == 1, windows

        root_html = inspector_http(inspector_port, "/")
        target_rows = re.findall(r"class=\\\"targetname\\\">(.*?)</div>.*?class=\\\"targeturl\\\">(.*?)</div>", root_html, re.DOTALL)
        http = {"root_targets": target_rows, "json": inspector_http(inspector_port, "/json")[:200], "json_list": inspector_http(inspector_port, "/json/list")[:200], "json_version": inspector_http(inspector_port, "/json/version")[:200]}
        print("INSPECTOR-HTTP:", json.dumps(http, sort_keys=True), flush=True)
        results = {}
        child = None
        for number in range(1, 7):
            path = f"/socket/1/{number}/WebPage"
            try:
                candidate = ws_client(inspector_port, path)
                child_clients.append(candidate)
                state = json.loads(candidate.evaluate("JSON.stringify({href:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,240)})"))
                results[path] = {"ok": True, **state}
                if "window=add" in state.get("href", "") or "Add Download" in state.get("text", ""):
                    child = candidate
                    break
                candidate.close()
                child_clients.pop()
            except Exception as error:
                results[path] = {"ok": False, "error": str(error)}
        print("INSPECTOR-SOCKETS:", json.dumps(results, sort_keys=True), flush=True)
        if child is None:
            print("CHILD-WINDOW-INSPECTOR-PROBE: BLOCKED (no child page exposed by candidate WebKit sockets)", flush=True)
            return 2

        state = json.loads(child.evaluate("JSON.stringify({href:location.href,text:document.body?.innerText||'',buttons:[...document.querySelectorAll('button')].map(button=>button.textContent?.trim()||'')})"))
        print("CHILD-DOM:", json.dumps(state, sort_keys=True), flush=True)
        assert "Add Download" in state["text"], state
        job = support.wait_job(db, next(item["id"] for item in support.read_jobs(db) if item.get("source") == source), lambda row: row.get("state") in {"finalizing", "completed"}, timeout=60)
        child.evaluate("[...document.querySelectorAll('button')].find(button=>button.textContent?.trim()==='Download')?.click()")
        completed = support.wait_job(db, job["id"], lambda row: row.get("state") == "completed", timeout=60)
        assert Path(completed["destination"]).exists()
        assert Path(completed["destination"]).read_bytes() == PAYLOAD
        print(f"CHILD-WINDOW-INSPECTOR-PROBE: PASS (job={job['id']}, bytes={len(PAYLOAD)}, sha256={hashlib.sha256(PAYLOAD).hexdigest()})", flush=True)
        return 0
    finally:
        for candidate in child_clients:
            try:
                candidate.close()
            except Exception:
                pass
        if main_client is not None:
            main_client.close()
        if forward is not None and forward.poll() is None:
            support.terminate_only(forward, "child inspector forwarding process")
        if app is not None:
            support.terminate_only(app, "child inspector app")
        if app_log is not None:
            app_log.close()
        server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "child inspector Xvfb")
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"CHILD-WINDOW-INSPECTOR-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
