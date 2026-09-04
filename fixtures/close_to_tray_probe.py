#!/usr/bin/env python3
"""Real close-to-tray proof for the resident application."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True
from segmented_restart_probe import app_env, db_path, read_jobs, terminate_only, wait_job, wait_inspector, wait_tauri
import hls_fmp4_probe as hls

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
PAYLOAD_SIZE = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024


class SlowServer:
    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object):
                return

            def do_GET(self):
                with owner.lock:
                    owner.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(PAYLOAD_SIZE))
                self.end_headers()
                block = bytes((index * 17 + 3) % 256 for index in range(CHUNK_SIZE))
                remaining = PAYLOAD_SIZE
                while remaining:
                    try:
                        self.wfile.write(block[: min(CHUNK_SIZE, remaining)])
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= min(CHUNK_SIZE, remaining)
                    time.sleep(0.003)

        self.lock = threading.Lock()
        self.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def process_count_for_home(home: str) -> int:
    count = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            environ = Path("/proc", entry, "environ").read_bytes().split(b"\0")
            cmdline = Path("/proc", entry, "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if f"HOME={home}".encode() in environ and cmdline and cmdline[0].decode(errors="replace") == BIN:
            count += 1
    return count


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        last = read_jobs(db)
        for job in last:
            if job.get("source") == source:
                return job
        time.sleep(0.05)
    raise RuntimeError(f"capture job did not appear: {last}")


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-close-to-tray-")
    xvfb = None
    server = None
    resident = None
    forwarded = None
    client = None
    log_handle = None
    try:
        xvfb, display = hls.start_xvfb()
        import segmented_restart_probe as support
        support.DISPLAY = display
        server = SlowServer()
        source = f"http://127.0.0.1:{server.port}/close-to-tray.bin"
        inspector_port = support.free_port()
        log_handle = open(os.path.join(home, "resident.log"), "wb")
        resident = subprocess.Popen([BIN], env=app_env(home, inspector_port), stdout=log_handle, stderr=subprocess.STDOUT)
        client = wait_inspector(inspector_port, timeout=30)
        wait_tauri(client)
        assert resident.poll() is None, resident.poll()
        assert process_count_for_home(home) == 1, process_count_for_home(home)

        raw = json.dumps({"type": "capture-acquisition", "payload": {"source": source, "name": "close-to-tray.bin"}}, separators=(",", ":"))
        forwarded = subprocess.Popen([BIN, "--capture", raw], env=app_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert forwarded.wait(timeout=20) == 0
        db = db_path(home)
        job = wait_source_job(db, source)
        job = wait_job(db, job["id"], lambda current: current.get("state") == "downloading" and current.get("downloaded", 0) > 0, timeout=30)
        assert resident.poll() is None, resident.poll()

        close_result = client.evaluate("(document.querySelector('button[aria-label=\\\"Close\\\"]')?.click(), 'clicked')")
        assert close_result == "clicked", close_result
        time.sleep(0.3)
        visible_result = client.evaluate("window.__TAURI_INTERNALS__.invoke('plugin:window|is_visible', {label: 'main'})")
        tree = subprocess.run(["xwininfo", "-root", "-tree"], env=app_env(home), capture_output=True, text=True, check=True).stdout
        match = re.search(r'^\s*(0x[0-9a-f]+) "Download Manager":', tree, re.MULTILINE)
        assert match, tree
        manager_info = subprocess.run(["xwininfo", "-id", match.group(1)], env=app_env(home), capture_output=True, text=True, check=True).stdout
        map_state = next((line.strip() for line in manager_info.splitlines() if line.strip().startswith("Map State:")), "missing")
        print(f"CLOSE-TO-TRAY-DIAGNOSTIC: plugin_is_visible={visible_result!r}; manager_id={match.group(1)}; {map_state}", flush=True)
        assert map_state == "Map State: IsUnMapped", manager_info
        assert resident.poll() is None, resident.poll()
        assert process_count_for_home(home) == 1, process_count_for_home(home)

        finished = wait_job(db, job["id"], lambda current: current.get("state") == "finalizing" and current.get("downloaded") == PAYLOAD_SIZE, timeout=30)
        assert finished.get("provisional") is True, finished
        assert server.requests == 1, server.requests
        print(f"CLOSE-TO-TRAY: PASS (main_visible={visible_result!r}, resident_pid={resident.pid}, processes=1, state={finished['state']}, bytes={finished['downloaded']}, source_requests={server.requests})", flush=True)
        print("CLOSE-TO-TRAY-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if forwarded is not None and forwarded.poll() is None:
            terminate_only(forwarded, "close-to-tray forwarding process")
        if resident is not None:
            terminate_only(resident, "close-to-tray resident process")
        if log_handle is not None:
            log_handle.close()
        if server is not None:
            server.stop()
        if xvfb is not None:
            terminate_only(xvfb, "close-to-tray Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"CLOSE-TO-TRAY-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
