#!/usr/bin/env python3
"""Real-binary HLS EXT-X-BYTERANGE acquisition proof."""

from __future__ import annotations

import hashlib
import json
import os
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
RESOURCE = bytes((index * 37 + 11) % 256 for index in range(4096))
RANGES = [(0, 5), (5, 6), (20, 7)]
MANIFEST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:4\n"
    "#EXT-X-TARGETDURATION:2\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXTINF:2,\n"
    "#EXT-X-BYTERANGE:5@0\n"
    "/media.bin\n"
    "#EXTINF:2,\n"
    "#EXT-X-BYTERANGE:6\n"
    "/media.bin\n"
    "#EXTINF:2,\n"
    "#EXT-X-BYTERANGE:7@20\n"
    "/media.bin\n"
    "#EXT-X-ENDLIST\n"
).encode()


class ByteRangeServer:
    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send_body(self, body: bytes, status: int, content_type: str):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_GET(self):
                path = urlsplit(self.path).path
                range_header = self.headers.get("Range")
                with owner.lock:
                    owner.requests.append((path, range_header))
                if path == "/hls/byterange.m3u8":
                    return self.send_body(MANIFEST, 200, "application/vnd.apple.mpegurl")
                if path != "/media.bin":
                    return self.send_body(b"not found", 404, "text/plain")
                if range_header is None:
                    return self.send_body(RESOURCE, 200, "video/mp2t")
                value = range_header.removeprefix("bytes=")
                start_text, end_text = value.split("-", 1)
                start = int(start_text)
                end = int(end_text) if end_text else len(RESOURCE) - 1
                if start < 0 or end < start or end >= len(RESOURCE):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(RESOURCE)}")
                    self.end_headers()
                    return
                body = RESOURCE[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(RESOURCE)}")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        self.lock = threading.Lock()
        self.requests: list[tuple[str, str | None]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def snapshot(self) -> list[tuple[str, str | None]]:
        with self.lock:
            return list(self.requests)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def db_path(home: str) -> str:
    return os.path.join(home, ".local", "share", "com.downloadmanager.app", "download-manager.db")


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


def wait_job(db: str, job_id: str, predicate, timeout: float = 60.0) -> dict:
    return support.wait_job(db, job_id, predicate, timeout)


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-hls-byterange-")
    xvfb = None
    proxy = None
    app = None
    client = None
    app_log = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        proxy = ByteRangeServer()
        source = f"http://127.0.0.1:{proxy.port}/hls/byterange.m3u8"
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
        app_log = None
        try:
            client = support.wait_inspector(inspector_port)
            support.wait_tauri(client)
        except Exception:
            print(f"APP: poll={app.poll()}", flush=True)
            if os.path.exists(app_log_path):
                print(f"APP-LOG: {open(app_log_path, encoding='utf-8', errors='replace').read()}", flush=True)
            raise
        support.invoke(
            client,
            "create_provisional",
            {"input": {"source": source, "name": "byterange.ts", "media": True, "maxConnections": 2}},
        )
        db = db_path(root)
        created = wait_source_job(db, source)
        job_id = created["id"]
        ready = wait_job(db, job_id, lambda job: job.get("state") == "finalizing" and job.get("segments", {}).get("completed") == 3)
        destination = os.path.join(root, "Downloads", "byterange.ts")
        support.invoke(
            client,
            "commit_provisional",
            {"id": job_id, "input": {"name": "byterange.ts", "destination": destination, "maxConnections": 2}},
        )
        completed = wait_job(db, job_id, lambda job: job.get("state") == "completed")
        requests = proxy.snapshot()
        client.close()
        client = None
        expected = b"".join(RESOURCE[start : start + length] for start, length in RANGES)
        output = completed["destination"]
        actual = open(output, "rb").read()
        assert actual == expected, (len(actual), len(expected))
        media_ranges = [header for path, header in requests if path == "/media.bin" and header is not None]
        expected_ranges = [f"bytes={start}-{start + length - 1}" for start, length in RANGES]
        assert sorted(media_ranges) == sorted(expected_ranges), (media_ranges, expected_ranges)
        assert sum(path == "/hls/byterange.m3u8" for path, _ in requests) == 1, requests
        print(
            f"HLS-BYTERANGE: PASS (ready={ready['segments']['completed']}/3, output={len(actual)} bytes, sha256={hashlib.sha256(actual).hexdigest()}, ranges={json.dumps(media_ranges)})",
            flush=True,
        )
        print("HLS-BYTERANGE-PROBE: PASS", flush=True)
        return 0
    finally:
        if app_log is not None:
            app_log.close()
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "HLS byte-range app")
        if proxy is not None:
            proxy.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "HLS byte-range Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"HLS-BYTERANGE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
