#!/usr/bin/env python3
"""Red/green proof for SPEC §9.1 progressive-media byte preservation."""

from __future__ import annotations

import hashlib
import json
import os
import socket
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


class FileProxy:
    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                with proxy.lock:
                    proxy.counts[parsed.path] += 1
                target = f"http://127.0.0.1:{proxy.upstream_port}{parsed.path}"
                if parsed.query:
                    target += "?" + parsed.query
                try:
                    with urlopen(Request(target), timeout=30) as response:
                        data = response.read()
                        status = response.status
                        content_type = response.headers.get("Content-Type", "application/octet-stream")
                except Exception as error:
                    reader = getattr(error, "read", None)
                    raw = reader() if callable(reader) else None
                    data = raw if isinstance(raw, bytes) else str(error).encode()
                    status = getattr(error, "code", 502)
                    content_type = "text/plain"
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


def wait_job(db: str, predicate, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in read_jobs(db) if predicate(job)), None)
        if last is not None:
            return last
        time.sleep(0.1)
    raise RuntimeError(f"progressive job did not reach expected state: {last}")


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


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-progressive-mp4-")
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        support.wait_http(f"http://127.0.0.1:{upstream_port}/media/real.mp4")
        proxy = FileProxy(upstream_port)
        source = f"http://127.0.0.1:{proxy.port}/media/real.mp4"
        with urlopen(source, timeout=30) as response:
            original = response.read()
        original_hash = hashlib.sha256(original).hexdigest()
        proxy.counts.clear()

        inspector_port = support.free_port()
        app_log = open(os.path.join(root, "app.log"), "wb")
        app = subprocess.Popen(
            [BIN], env=support.app_env(root, inspector_port),
            stdout=app_log, stderr=subprocess.STDOUT,
        )
        app_log.close()
        try:
            client = support.wait_inspector(inspector_port)
            support.wait_tauri(client)
        except Exception:
            print(f"APP: poll={app.poll()}", flush=True)
            log_path = os.path.join(root, "app.log")
            if os.path.exists(log_path):
                print(f"APP-LOG: {open(log_path, encoding='utf-8', errors='replace').read()}", flush=True)
            raise
        db = db_path(root)
        support.invoke(
            client,
            "create_provisional",
            {"input": {"source": source, "name": "progressive.mp4", "media": True, "maxConnections": 2}},
        )
        created = wait_job(db, lambda job: job.get("source") == source)
        ready = wait_job(
            db,
            lambda job: job.get("id") == created["id"]
            and job.get("state") == "finalizing"
            and job.get("downloaded") == len(original),
        )
        temp_hash = hashlib.sha256(open(ready["tempPath"], "rb").read()).hexdigest()
        assert temp_hash == original_hash, (temp_hash, original_hash)
        destination = os.path.join(root, "Downloads", "progressive.mp4")
        support.invoke(
            client,
            "commit_provisional",
            {"id": created["id"], "input": {"name": "progressive.mp4", "destination": destination}},
        )
        completed = wait_job(db, lambda job: job.get("id") == created["id"] and job.get("state") == "completed")
        client.close()
        client = None
        output = completed["destination"]
        output_hash = hashlib.sha256(open(output, "rb").read()).hexdigest()
        assert output_hash == original_hash, {
            "output": output_hash,
            "original": original_hash,
            "job": completed,
            "requests": dict(proxy.counts),
        }
        print(f"PROGRESSIVE-MP4: PASS (bytes={len(original)}, sha256={output_hash})", flush=True)
        print(f"REQUESTS: {json.dumps(dict(proxy.counts), sort_keys=True)}", flush=True)
        print(f"E2E-ROOT: {root}", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "progressive app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "progressive fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "progressive Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PROGRESSIVE-MP4: FAIL: {error}", file=sys.stderr, flush=True)
        raise
