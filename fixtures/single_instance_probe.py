#!/usr/bin/env python3
"""Real two-process single-instance forwarding proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading

sys.dont_write_bytecode = True
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
PAYLOAD = bytes((index * 19 + 7) % 256 for index in range(32 * 1024))


class FileServer:
    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object):
                return

            def do_GET(self):
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
        if (f"HOME={home}".encode() in environ and cmdline and cmdline[0].decode(errors="replace") == BIN):
            count += 1
    return count


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-single-instance-")
    xvfb = None
    server = None
    first = None
    second = None
    first_log = None
    second_log = None
    client = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        server = FileServer()
        source = f"http://127.0.0.1:{server.port}/forwarded.bin"
        inspector_port = support.free_port()
        first_log = open(os.path.join(root, "first.log"), "wb")
        first = subprocess.Popen([BIN], env=support.app_env(root, inspector_port), stdout=first_log, stderr=subprocess.STDOUT)
        try:
            client = support.wait_inspector(inspector_port, timeout=30)
        except Exception:
            first_log.flush()
            raise RuntimeError(f"resident Inspector startup failed; log:\n{Path(root, 'first.log').read_text(errors='replace')}")
        support.wait_tauri(client)
        assert first.poll() is None, first.poll()
        assert process_count_for_home(root) == 1, process_count_for_home(root)

        raw = json.dumps({"type": "capture-acquisition", "payload": {"source": source, "name": "forwarded.bin"}}, separators=(",", ":"))
        second_log = open(os.path.join(root, "second.log"), "wb")
        second = subprocess.Popen([BIN, "--capture", raw], env=support.app_env(root), stdout=second_log, stderr=subprocess.STDOUT)
        second_code = second.wait(timeout=20)
        assert second_code == 0, second_code
        assert first.poll() is None, first.poll()
        assert process_count_for_home(root) == 1, process_count_for_home(root)

        db = hls.db_path(root)
        job = hls.wait_source_job(db, source)
        stable = support.wait_job(db, job["id"], lambda current: current.get("state") == "finalizing" and current.get("downloaded") == len(PAYLOAD))
        assert stable["provisional"] is True, stable
        assert server.requests == 1, server.requests
        print(f"SINGLE-INSTANCE: PASS (second_exit={second_code}, resident_pid={first.pid}, jobs=1, state={stable['state']}, bytes={stable['downloaded']}, source_requests={server.requests})", flush=True)
        print("SINGLE-INSTANCE-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if second is not None and second.poll() is None:
            support.terminate_only(second, "single-instance second process")
        if first is not None:
            support.terminate_only(first, "single-instance resident process")
        if first_log is not None:
            first_log.close()
        if second_log is not None:
            second_log.close()
        if server is not None:
            server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "single-instance Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SINGLE-INSTANCE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
