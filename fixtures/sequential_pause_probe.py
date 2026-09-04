#!/usr/bin/env python3
"""Prove pause during sequential segment fallback preserves the job."""

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
SEGMENTS = ["seg0.ts", "seg1.ts", "seg2.ts", "seg3.ts"]
SEGMENT_SIZE = 256 * 1024


class SlowSequentialServer:
    def __init__(self):
        owner = self
        playlist = "\n".join(["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", *[f"#EXTINF:1,\n{name}" for name in SEGMENTS], "#EXT-X-ENDLIST", ""]).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object):
                return

            def do_GET(self):
                name = Path(urlsplit(self.path).path).name
                if name == "sequential-pause.m3u8":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(playlist)))
                    self.end_headers()
                    self.wfile.write(playlist)
                    return
                if name not in SEGMENTS:
                    self.send_error(404)
                    return
                with owner.lock:
                    owner.requests[name] += 1
                    busy = owner.active
                    if busy:
                        owner.rejected += 1
                    else:
                        owner.active = True
                if busy:
                    body = b"busy"
                    self.send_response(429)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                payload = bytes((index * 31 + SEGMENTS.index(name)) % 256 for index in range(SEGMENT_SIZE))
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    for offset in range(0, len(payload), 4096):
                        self.wfile.write(payload[offset : offset + 4096])
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with owner.lock:
                        owner.active = False

        self.lock = threading.Lock()
        self.active = False
        self.rejected = 0
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


def wait_source_job(db: str, source: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        last = support.read_jobs(db)
        for job in last:
            if job.get("source") == source:
                return job
        time.sleep(0.05)
    raise RuntimeError(f"source job did not appear: {last}")


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-sequential-pause-")
    xvfb = None
    server = None
    resident = None
    forwarded = None
    client = None
    log_handle = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        server = SlowSequentialServer()
        source = f"http://127.0.0.1:{server.port}/sequential-pause.m3u8"
        inspector_port = support.free_port()
        log_handle = open(os.path.join(home, "resident.log"), "wb")
        resident = subprocess.Popen([BIN], env=support.app_env(home, inspector_port), stdout=log_handle, stderr=subprocess.STDOUT)
        try:
            client = support.wait_inspector(inspector_port, timeout=30)
        except Exception as error:
            log_handle.flush()
            log = Path(home, "resident.log").read_text(errors="replace")
            raise RuntimeError(f"Inspector startup failed: app_poll={resident.poll()} display={display} port={inspector_port} log={log!r}") from error
        support.wait_tauri(client)
        support.invoke(client, "update_settings", {"patch": {"retryAutomatically": False, "maxRetries": 0}})
        raw = json.dumps({"type": "capture-acquisition", "payload": {"source": source, "name": "sequential-pause.ts", "media": True, "maxConnections": 4}}, separators=(",", ":"))
        forwarded = subprocess.Popen([BIN, "--capture", raw], env=support.app_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert forwarded.wait(timeout=20) == 0
        db = support.db_path(home)
        job = wait_source_job(db, source)
        job = support.wait_job(db, job["id"], lambda current: any("retrying sequentially" in event.get("message", "").lower() for event in current.get("events", [])), timeout=30)
        support.invoke(client, "pause_job", {"id": job["id"]})
        paused = support.wait_job(db, job["id"], lambda current: current.get("state") == "paused", timeout=20)
        assert paused.get("error") is None, paused
        assert 0 < paused.get("segments", {}).get("completed", 0) < len(SEGMENTS), paused
        assert resident.poll() is None, resident.poll()
        print(f"SEQUENTIAL-PAUSE: PASS (state={paused['state']}, completed={paused['segments']['completed']}/{len(SEGMENTS)}, downloaded={paused['downloaded']}, rejected_overlaps={server.rejected}, requests={dict(server.requests)})", flush=True)
        print("SEQUENTIAL-PAUSE-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if forwarded is not None and forwarded.poll() is None:
            support.terminate_only(forwarded, "sequential-pause forwarding process")
        if resident is not None:
            support.terminate_only(resident, "sequential-pause resident process")
        if log_handle is not None:
            log_handle.close()
        if server is not None:
            server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "sequential-pause Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SEQUENTIAL-PAUSE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
