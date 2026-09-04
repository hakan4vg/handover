#!/usr/bin/env python3
"""Real proof that three rapid captures create three Add Download windows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
SOURCES = ["one.bin", "two.bin", "three.bin"]
PAYLOAD_SIZE = 128 * 1024


class CaptureServer:
    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object):
                return

            def do_GET(self):
                name = Path(urlsplit(self.path).path).name
                if name not in SOURCES:
                    self.send_error(404)
                    return
                with owner.lock:
                    owner.requests[name] += 1
                marker = SOURCES.index(name) + 1
                payload = bytes((index * 23 + marker) % 256 for index in range(PAYLOAD_SIZE))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.lock = threading.Lock()
        self.requests = Counter()
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


def add_window_count(display: str) -> int:
    env = dict(os.environ, DISPLAY=display)
    tree = subprocess.run(["xwininfo", "-root", "-tree"], env=env, capture_output=True, text=True, check=True).stdout
    return sum(1 for line in tree.splitlines() if '"Add Download"' in line)


def wait_jobs(db: str, sources: list[str], timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = support.read_jobs(db)
        selected = [job for job in last if job.get("source") in sources]
        if len(selected) == len(sources):
            return selected
        time.sleep(0.05)
    raise RuntimeError(f"capture jobs did not all appear: {last}")


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-multiple-add-windows-")
    xvfb = None
    server = None
    resident = None
    forwarded: list[subprocess.Popen[bytes]] = []
    client = None
    log_handle = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        server = CaptureServer()
        sources = [f"http://127.0.0.1:{server.port}/{name}" for name in SOURCES]
        inspector_port = support.free_port()
        log_handle = open(os.path.join(home, "resident.log"), "wb")
        resident = subprocess.Popen([BIN], env=support.app_env(home, inspector_port), stdout=log_handle, stderr=subprocess.STDOUT)
        client = support.wait_inspector(inspector_port, timeout=30)
        support.wait_tauri(client)
        assert resident.poll() is None, resident.poll()

        for index, source in enumerate(sources):
            raw = json.dumps({"type": "capture-acquisition", "payload": {"source": source, "name": SOURCES[index]}}, separators=(",", ":"))
            forwarded.append(subprocess.Popen([BIN, "--capture", raw], env=support.app_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for process in forwarded:
            assert process.wait(timeout=20) == 0

        db = support.db_path(home)
        jobs = wait_jobs(db, sources)
        deadline = time.time() + 20
        count = 0
        while time.time() < deadline:
            count = add_window_count(display)
            if count == len(sources):
                break
            time.sleep(0.1)
        assert count == len(sources), count
        assert resident.poll() is None, resident.poll()
        assert process_count_for_home(home) == 1, process_count_for_home(home)
        assert all(job.get("source") in sources for job in jobs), jobs
        assert len({job.get("id") for job in jobs}) == len(sources), jobs
        assert server.requests == Counter(dict.fromkeys(SOURCES, 1)), server.requests
        print(f"MULTIPLE-ADD-WINDOWS: PASS (windows={count}, jobs={len(jobs)}, processes=1, source_requests={dict(server.requests)})", flush=True)
        print("MULTIPLE-ADD-WINDOWS-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        for process in forwarded:
            if process.poll() is None:
                support.terminate_only(process, "multiple-window forwarding process")
        if resident is not None:
            support.terminate_only(resident, "multiple-window resident process")
        if log_handle is not None:
            log_handle.close()
        if server is not None:
            server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "multiple-window Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MULTIPLE-ADD-WINDOWS-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
