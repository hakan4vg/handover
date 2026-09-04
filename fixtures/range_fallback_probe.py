#!/usr/bin/env python3
"""Real-binary proof of ranged acquisition falling back to one stream.

The disposable source accepts the initial bytes=0-0 probe, returns HTTP 429
for every other Range request, and serves the complete resource for a plain
GET. This models a host that advertises ranges but rejects parallel/nonzero
range traffic.
"""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = support.BIN
RESOURCE = bytes((index * 31 + 17) % 256 for index in range(3 * 1024 * 1024 + 123))


class RateLimitedRangeServer:
    def __init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send_bytes(self, body: bytes, status: int, content_type: str = "application/octet-stream") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                if status == 429:
                    self.send_header("Retry-After", "3")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_GET(self):
                if urlsplit(self.path).path != "/range-fallback.bin":
                    return self.send_bytes(b"not found", 404, "text/plain")
                range_header = self.headers.get("Range")
                with owner.lock:
                    owner.requests.append(range_header)
                if range_header == "bytes=0-0":
                    self.send_response(206)
                    self.send_header("Content-Length", "1")
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes 0-0/{len(RESOURCE)}")
                    self.end_headers()
                    self.wfile.write(RESOURCE[:1])
                    return
                if range_header is not None:
                    return self.send_bytes(b"range temporarily unavailable", 429)
                return self.send_bytes(RESOURCE, 200)

        self.lock = threading.Lock()
        self.requests: list[str | None] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def snapshot(self) -> list[str | None]:
        with self.lock:
            return list(self.requests)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        jobs = support.read_jobs(db)
        matches = [job for job in jobs if job.get("source") == source]
        if matches:
            return matches[-1]
        last = jobs
        time.sleep(0.1)
    raise RuntimeError(f"capture did not create a job: {last}")


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-range-fallback-")
    xvfb = None
    server = None
    app = None
    client = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        server = RateLimitedRangeServer()
        source = f"http://127.0.0.1:{server.port}/range-fallback.bin"
        inspector_port = support.free_port()
        app_log_path = os.path.join(root, "app.log")
        app_log = open(app_log_path, "wb")
        app = subprocess.Popen(
            [BIN],
            env=support.app_env(root, inspector_port),
            stdout=app_log,
            stderr=subprocess.STDOUT,
        )
        app_log.close()
        client = support.wait_inspector(inspector_port)
        support.wait_tauri(client)
        support.invoke(client, "create_provisional", {"input": {"source": source, "name": "range-fallback.bin", "maxConnections": 3}})
        db = support.wait_db(root)
        created = wait_source_job(db, source)
        job_id = created["id"]
        ready = support.wait_job(db, job_id, lambda job: job.get("state") == "finalizing", timeout=120)
        destination = os.path.join(root, "Downloads", "range-fallback.bin")
        support.invoke(client, "commit_provisional", {"id": job_id, "input": {"name": "range-fallback.bin", "destination": destination, "maxConnections": 3}})
        completed = support.wait_job(db, job_id, lambda job: job.get("state") == "completed", timeout=120)
        requests = server.snapshot()
        actual = open(destination, "rb").read()
        assert actual == RESOURCE, (len(actual), len(RESOURCE))
        assert "bytes=0-0" in requests, requests
        assert any(value is not None and value != "bytes=0-0" for value in requests), requests
        assert None in requests, requests
        assert completed.get("mode") == "single-stream", completed
        assert completed.get("resumable") is False, completed
        events = " | ".join(event.get("message", "") for event in completed.get("events", []))
        assert "retrying as one stream" in events.lower(), events
        print(
            f"RANGE-FALLBACK: PASS (ready={ready['downloaded']} bytes, output={len(actual)} bytes, sha256={hashlib.sha256(actual).hexdigest()}, ranged_requests={sum(value is not None for value in requests)}, plain_gets={requests.count(None)}, mode={completed['mode']}, resumable={completed['resumable']})",
            flush=True,
        )
        print("RANGE-FALLBACK-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "range fallback app")
        if server is not None:
            server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "range fallback Xvfb")
        if os.environ.get("DM_KEEP_RANGE_FALLBACK") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RANGE-FALLBACK-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
