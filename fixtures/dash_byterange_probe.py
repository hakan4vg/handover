#!/usr/bin/env python3
"""Real-binary static DASH SegmentList byte-range proof."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
RESOURCE = bytes((index * 37 + 11) % 256 for index in range(64))
MANIFEST = b'''<?xml version="1.0"?>
<MPD type="static"><Period><AdaptationSet contentType="video">
  <SegmentList>
    <Initialization sourceURL="media.bin" range="30-33"/>
    <SegmentURL media="media.bin" mediaRange="0-4"/>
    <SegmentURL media="media.bin" mediaRange="20-25"/>
  </SegmentList>
  <Representation id="video"><BaseURL>/</BaseURL></Representation>
</AdaptationSet></Period></MPD>
'''


class DashRangeServer:
    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object):
                return

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                range_header = self.headers.get("Range")
                with owner.lock:
                    owner.requests.append((path, range_header))
                if path == "/manifest.mpd":
                    return self.send_body(MANIFEST, 200, "application/dash+xml")
                if path != "/media.bin":
                    return self.send_body(b"not found", 404, "text/plain")
                if range_header is None:
                    return self.send_body(RESOURCE, 200, "video/mp4")
                if not range_header.startswith("bytes=") or "-" not in range_header:
                    return self.send_body(b"bad range", 416, "text/plain")
                start_text, end_text = range_header[6:].split("-", 1)
                start = int(start_text)
                end = int(end_text)
                if start < 0 or end < start or end >= len(RESOURCE):
                    return self.send_body(b"bad range", 416, "text/plain")
                body = RESOURCE[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(RESOURCE)}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(body)

            def send_body(self, body: bytes, status: int, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.lock = threading.Lock()
        self.requests: list[tuple[str, str | None]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def snapshot(self) -> list[tuple[str, str | None]]:
        with self.lock:
            return list(self.requests)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def db_path(root: str) -> str:
    return os.path.join(root, ".config", "download-manager", "downloads.db")


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-dash-byterange-")
    xvfb = None
    proxy = None
    app = None
    client = None
    app_log = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        proxy = DashRangeServer()
        source = f"http://127.0.0.1:{proxy.port}/manifest.mpd"
        inspector_port = support.free_port()
        app_log = open(os.path.join(root, "app.log"), "wb")
        env = support.app_env(root, inspector_port)
        app = subprocess.Popen([BIN], env=env, stdout=app_log, stderr=subprocess.STDOUT)
        try:
            client = support.wait_inspector(inspector_port, timeout=30)
        except Exception:
            app_log.flush()
            raise RuntimeError(f"Inspector startup failed; app log:\n{Path(root, 'app.log').read_text(errors='replace')}")
        support.wait_tauri(client)
        support.invoke(client, "create_provisional", {"input": {"source": source, "name": "dash-range.m4s", "media": True, "maxConnections": 2}})
        db = hls.db_path(root)
        created = hls.wait_source_job(db, source)
        job_id = created["id"]
        ready = support.wait_job(db, job_id, lambda job: job.get("state") == "finalizing" and job.get("segments", {}).get("completed") == 3)
        destination = str(Path(root) / "Downloads" / "dash-range.m4s")
        support.invoke(client, "commit_provisional", {"id": job_id, "input": {"name": "dash-range.m4s", "destination": destination, "maxConnections": 2}})
        completed = support.wait_job(db, job_id, lambda job: job.get("state") == "completed")
        expected = RESOURCE[30:34] + RESOURCE[0:5] + RESOURCE[20:26]
        output = Path(destination).read_bytes()
        requests = proxy.snapshot()
        media_ranges = sorted(header for path, header in requests if path == "/media.bin" and header is not None)
        expected_ranges = sorted(["bytes=30-33", "bytes=0-4", "bytes=20-25"])
        assert ready["segments"]["completed"] == 3, ready
        assert completed["state"] == "completed", completed
        assert output == expected, (len(output), len(expected))
        assert media_ranges == expected_ranges, (media_ranges, expected_ranges)
        output_hash = hashlib.sha256(output).hexdigest()
        print(f"DASH-BYTERANGE: PASS (ready=3/3, output={len(output)} bytes, sha256={output_hash}, ranges={media_ranges})", flush=True)
        print("DASH-BYTERANGE-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "DASH byte-range app")
        if app_log is not None:
            app_log.close()
        if proxy is not None:
            proxy.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "DASH byte-range Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"DASH-BYTERANGE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
