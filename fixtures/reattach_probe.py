#!/usr/bin/env python3
"""Real-binary proof for SPEC §8.9 targeted reattach and identity safety."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import segmented_restart_probe as support

BIN = support.BIN
FIXTURE_SERVER = support.FIXTURE_SERVER
PARTIAL = 64 * 1024
TOTAL = 256 * 1024


class CountingProxy:
    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.counts: Counter[str] = Counter()
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                requested_range = self.headers.get("Range")
                with proxy.lock:
                    proxy.counts[parsed.path] += 1
                target = f"http://127.0.0.1:{proxy.upstream_port}{parsed.path}"
                if parsed.query:
                    target += "?" + parsed.query
                request_headers = {}
                if self.headers.get("Range"):
                    request_headers["Range"] = self.headers["Range"]
                try:
                    with urlopen(Request(target, headers=request_headers), timeout=30) as response:
                        data = response.read()
                        status = response.status
                        headers = {
                            name: response.headers.get(name)
                            for name in ("Content-Type", "ETag", "Last-Modified", "Accept-Ranges", "Content-Range")
                            if response.headers.get(name) is not None
                        }
                except Exception as error:
                    reader = getattr(error, "read", None)
                    raw = reader() if callable(reader) else None
                    data = raw if isinstance(raw, bytes) else str(error).encode()
                    status = getattr(error, "code", 502)
                    headers = {"Content-Type": "text/plain"}
                with proxy.lock:
                    proxy.requests.append({
                        "method": self.command,
                        "path": parsed.path,
                        "query": parsed.query,
                        "range": requested_range,
                        "status": status,
                        "content_range": headers.get("Content-Range"),
                        "etag": headers.get("ETag"),
                        "bytes": len(data),
                    })
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
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
            self.requests.clear()

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

    def requests_snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.requests)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def db_path(home: str) -> str:
    return os.path.join(home, ".local", "share", "com.downloadmanager.app", "download-manager.db")


def read_jobs(db: str) -> list[dict]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT payload FROM jobs").fetchall()
    return [json.loads(payload) for (payload,) in rows]


def wait_job(db: str, job_id: str, predicate, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for job in read_jobs(db):
            if job.get("id") == job_id and predicate(job):
                return job
        time.sleep(0.1)
    raise RuntimeError(f"job {job_id} did not reach expected state")


def wait_db(db: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(db):
            return
        time.sleep(0.1)
    raise RuntimeError("database did not appear")


def assert_range_requests(requests: list[dict], query: str, etag: str, remainder_start: int) -> None:
    assert [request["range"] for request in requests] == ["bytes=0-0", f"bytes={remainder_start}-{TOTAL - 1}"], requests
    assert [request["status"] for request in requests] == [206, 206], requests
    assert [request["content_range"] for request in requests] == [
        f"bytes 0-0/{TOTAL}",
        f"bytes {remainder_start}-{TOTAL - 1}/{TOTAL}",
    ], requests
    assert [request["bytes"] for request in requests] == [1, TOTAL - remainder_start], requests
    assert all(request["query"] == query for request in requests), requests
    assert all(request["etag"] == etag for request in requests), requests


def seed_job(db: str, home: str, source: str, job_id: str, name: str, data: bytes, etag: str, state: str = "paused"):
    temp_path = os.path.join(home, ".cache", "download-manager", "tmp", f"{job_id}.part")
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    with open(temp_path, "wb") as handle:
        handle.write(data[:PARTIAL])
        handle.truncate(TOTAL)
    job = {
        "id": job_id,
        "name": name,
        "source": source,
        "domain": "127.0.0.1",
        "kind": "document",
        "state": state,
        "progress": PARTIAL / TOTAL * 100,
        "downloaded": PARTIAL,
        "total": TOTAL,
        "speed": 0,
        "eta": "Paused",
        "connections": 0,
        "maxConnections": 2,
        "bandwidthLimit": None,
        "mode": "whole-object",
        "media": False,
        "mediaDetails": None,
        "mediaTracks": None,
        "destination": os.path.join(home, "Downloads", name),
        "tempPath": temp_path,
        "resumable": True,
        "mime": "application/octet-stream",
        "error": None,
        "created": "Just now",
        "started": None,
        "completed": None,
        "provisional": False,
        "completedRanges": [{"start": 0, "end": PARTIAL - 1}],
        "resourceIdentity": {"length": TOTAL, "etag": etag, "lastModified": None},
        "events": [],
    }
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO jobs (id, created_at, payload) VALUES (?, ?, ?)",
            (job_id, "Just now", json.dumps(job)),
        )
        connection.commit()
    return temp_path


def start_xvfb() -> tuple[subprocess.Popen[bytes], str]:
    for number in range(110, 200):
        socket_path = f"/tmp/.X11-unix/X{number}"
        if os.path.exists(socket_path):
            continue
        display = f":{number}"
        process = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x900x24", "-ac", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(f"Xvfb exited with {process.returncode}")
            if os.path.exists(socket_path):
                return process, display
            time.sleep(0.1)
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError(f"Xvfb did not become ready: {display}")
    raise RuntimeError("no disposable Xvfb display available")


def boot_and_stop(home: str):
    process = subprocess.Popen(
        [BIN], env=support.app_env(home, None),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_db(db_path(home))
    support.terminate_only(process, "reattach database bootstrap")


def send_capture(home: str, source: str, name: str, post_body: str | None = None):
    payload = {"source": source, "name": name}
    if post_body is not None:
        payload["postBody"] = post_body
    message = {"type": "capture-acquisition", "payload": payload}
    process = subprocess.Popen(
        [BIN, "--capture", json.dumps(message)],
        env=support.app_env(home, None),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    process.wait(timeout=20)
    assert process.returncode == 0, process.returncode


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-reattach-")
    xvfb = None
    upstream = None
    proxy = None
    app = None
    client = None
    try:
        xvfb, display = start_xvfb()
        support.DISPLAY = display
        upstream_port = support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(FIXTURE_SERVER),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        support.wait_http(f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1")
        proxy = CountingProxy(upstream_port)
        base = f"http://127.0.0.1:{proxy.port}/changed.bin"
        old_source = base + "?variant=1"
        renewed_source = base + "?variant=1&renewed=1"
        changed_source = base + "?variant=2&renewed=2"
        with urlopen(old_source) as response:
            variant_one = response.read()
        with urlopen(changed_source) as response:
            variant_two = response.read()
        assert len(variant_one) == TOTAL == len(variant_two)
        variant_one_hash = hashlib.sha256(variant_one).hexdigest()
        variant_two_hash = hashlib.sha256(variant_two).hexdigest()
        proxy.reset()

        boot_and_stop(home)
        db = db_path(home)
        first_temp = seed_job(db, home, old_source, "reattach-compatible", "reattached.bin", variant_one, '"v1"')
        second_temp = seed_job(db, home, old_source, "reattach-changed", "changed.bin", variant_one, '"v1"')
        inspector_port = support.free_port()
        app = subprocess.Popen(
            [BIN], env=support.app_env(home, inspector_port),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        client = support.wait_inspector(inspector_port)
        support.wait_tauri(client)

        support.invoke(client, "reattach_job", {"id": "reattach-compatible"})
        wait_job(db, "reattach-compatible", lambda job: job.get("state") == "pending")
        send_capture(home, renewed_source, "reattached.bin")
        compatible = wait_job(db, "reattach-compatible", lambda job: job.get("state") == "completed")
        compatible_hash = hashlib.sha256(open(compatible["destination"], "rb").read()).hexdigest()
        assert compatible_hash == variant_one_hash, (compatible_hash, variant_one_hash)
        assert len([job for job in read_jobs(db) if job.get("id") == "reattach-compatible"]) == 1
        assert not os.path.exists(first_temp)
        compatible_counts = proxy.snapshot()
        compatible_requests = proxy.requests_snapshot()
        assert compatible_counts.get("/changed.bin") == len(compatible_requests) == 2, compatible_counts
        assert_range_requests(compatible_requests, "variant=1&renewed=1", '"v1"', PARTIAL)
        print(f"COMPATIBLE-REQUESTS: {json.dumps(compatible_requests, sort_keys=True)}", flush=True)

        proxy.reset()
        support.invoke(client, "reattach_job", {"id": "reattach-changed"})
        wait_job(db, "reattach-changed", lambda job: job.get("state") == "pending")
        send_capture(home, changed_source, "changed.bin")
        changed = wait_job(db, "reattach-changed", lambda job: job.get("state") == "completed")
        changed_hash = hashlib.sha256(open(changed["destination"], "rb").read()).hexdigest()
        assert changed_hash == variant_two_hash, (changed_hash, variant_two_hash)
        assert changed_hash != variant_one_hash
        assert len([job for job in read_jobs(db) if job.get("id") == "reattach-changed"]) == 1
        assert not os.path.exists(second_temp)
        changed_counts = proxy.snapshot()
        changed_requests = proxy.requests_snapshot()
        assert changed_counts.get("/changed.bin") == len(changed_requests) == 2, changed_counts
        assert_range_requests(changed_requests, "variant=2&renewed=2", '"v2"', 1)
        print(f"CHANGED-REQUESTS: {json.dumps(changed_requests, sort_keys=True)}", flush=True)
        print(f"REATTACH-COMPATIBLE: PASS (one job, sha256={compatible_hash})", flush=True)
        print(f"REATTACH-IDENTITY-CHANGE: PASS (safe restart, sha256={changed_hash})", flush=True)
        print(f"REQUESTS: {json.dumps({'compatible': compatible_counts, 'changed': changed_counts}, sort_keys=True)}", flush=True)
        print(f"E2E-ROOT: {home}", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "reattach app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "reattach fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "reattach Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"REATTACH-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
