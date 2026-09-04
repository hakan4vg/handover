#!/usr/bin/env python3
"""Real-binary proof that startup invalidates stale segmented identities."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import reattach_probe as reattach
import segmented_restart_probe as support

JOB_ID = "startup-identity-invalidation"
STALE = b"stale-generation-fragment"


class IdentityDashProxy:
    """Serve a generation-2 manifest and count every forwarded request."""

    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                with proxy.lock:
                    proxy.counts[path] = proxy.counts.get(path, 0) + 1
                target = f"http://127.0.0.1:{proxy.upstream_port}{path}"
                if parsed.query:
                    target += "?" + parsed.query
                try:
                    with urlopen(Request(target), timeout=30) as response:
                        data = response.read()
                        status = response.status
                        content_type = response.headers.get("Content-Type", "application/octet-stream")
                except HTTPError as error:
                    data = error.read()
                    status = error.code
                    content_type = error.headers.get("Content-Type", "application/octet-stream")
                if path == "/dash/manifest.mpd" and "generation=2" in parsed.query and status == 200:
                    body = data.decode()
                    for name in ("v-init.mp4", "v-0.m4s", "v-1.m4s", "v-2.m4s", "a-init.mp4", "a-0.m4s"):
                        body = body.replace(f'"{name}"', f'"{name}?generation=2"')
                    data = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self):
        with self.lock:
            self.counts.clear()

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def seed_stale_finalizing_job(db: str, home: str, proxy_base: str, fragments: dict[str, bytes]) -> str:
    temp = support.seed_job(db, home, proxy_base, JOB_ID, "identity-recovered.mp4", fragments)
    segment_dir = Path(temp + ".segments")
    for global_index, (_kind, path) in enumerate(support.SEGMENTS):
        track = 0 if global_index < 4 else 1
        local_index = global_index if track == 0 else global_index - 4
        segment_path = segment_dir / f"{track:02}" / f"{local_index:08}.part"
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        segment_path.write_bytes(STALE)
    total_bytes = sum(len(fragments[path]) for _kind, path in support.SEGMENTS)
    generation_two_source = f"{proxy_base}/dash/manifest.mpd?generation=2"
    with sqlite3.connect(db) as connection:
        (payload,) = connection.execute("SELECT payload FROM jobs WHERE id = ?", (JOB_ID,)).fetchone()
        job = json.loads(payload)
        job.update({
            "source": generation_two_source,
            "state": "finalizing",
            "progress": 100.0,
            "downloaded": total_bytes,
            "connections": 0,
            "eta": "Finalizing",
            "segments": {"completed": 6, "total": 6, "identity": support.fnv_segment_identity(proxy_base)},
        })
        connection.execute("UPDATE jobs SET payload = ? WHERE id = ?", (json.dumps(job), JOB_ID))
        connection.commit()
    return temp


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-startup-identity-")
    xvfb = None
    upstream = None
    proxy = None
    app = None
    try:
        xvfb, display = reattach.start_xvfb()
        support.DISPLAY = display
        upstream_port = support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, support.FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(support.FIXTURE_SERVER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        support.wait_http(f"http://127.0.0.1:{upstream_port}/dash/manifest.mpd")
        proxy = IdentityDashProxy(upstream_port)
        proxy_base = f"http://127.0.0.1:{proxy.port}"
        fragments = {}
        for _kind, path in support.SEGMENTS:
            with urlopen(f"{proxy_base}{path}", timeout=30) as response:
                fragments[path] = response.read()
        reference, reference_hash = support.make_reference(root, fragments)
        proxy.reset()

        reattach.boot_and_stop(root)
        db = support.wait_db(root)
        temp_path = seed_stale_finalizing_job(db, root, proxy_base, fragments)
        app = subprocess.Popen(
            [support.BIN],
            env=support.app_env(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        completed = reattach.wait_job(
            db,
            JOB_ID,
            lambda job: job.get("state") == "completed",
            timeout=60.0,
        )
        output = Path(completed["destination"])
        output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        counts = proxy.snapshot()
        assert output_hash == reference_hash, (output_hash, reference_hash)
        assert counts.get("/dash/manifest.mpd") == 1, counts
        for _kind, path in support.SEGMENTS:
            assert counts.get(path) == 1, counts
        assert len([job for job in reattach.read_jobs(db) if job.get("id") == JOB_ID]) == 1
        assert not Path(temp_path).exists()
        assert not Path(temp_path + ".segments").exists()
        print(f"STARTUP-IDENTITY-INVALIDATION: PASS (job={JOB_ID}, sha256={output_hash})", flush=True)
        print(f"STARTUP-IDENTITY-REQUESTS: {counts}", flush=True)
        print(f"REFERENCE: {reference} sha256={reference_hash}", flush=True)
        print(f"E2E-ROOT: {root}", flush=True)
        return 0
    finally:
        if app is not None:
            support.terminate_only(app, "startup identity app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "startup identity fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "startup identity Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STARTUP-IDENTITY-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
