#!/usr/bin/env python3
"""Real-binary HLS fMP4 + alternate-audio acquisition proof.

The proxy supplies only HLS manifests. Every media byte comes from the existing
finite multi-track DASH fixture's real fMP4 files, so this exercises HLS
EXT-X-MAP parsing, alternate audio playlist discovery, parallel acquisition,
and native FFmpeg muxing without external media sites.
"""

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
SEGMENTS = [
    "/dash/v-init.mp4",
    "/dash/v-0.m4s",
    "/dash/v-1.m4s",
    "/dash/v-2.m4s",
    "/dash/a-init.mp4",
    "/dash/a-0.m4s",
]


class HlsProxy:
    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send_body(self, body: bytes, content_type: str = "application/octet-stream"):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                with proxy.lock:
                    proxy.counts[path] += 1
                if path == "/hls/master.m3u8":
                    return self.send_body(
                        b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="fixture",URI="audio.m3u8"\n'
                        b'#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio"\n'
                        b"video.m3u8\n",
                        "application/vnd.apple.mpegurl",
                    )
                if path == "/hls/video.m3u8":
                    return self.send_body(
                        b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
                        b'#EXT-X-MAP:URI="/dash/v-init.mp4"\n'
                        b"#EXTINF:4.0,\n/dash/v-0.m4s\n"
                        b"#EXTINF:4.0,\n/dash/v-1.m4s\n"
                        b"#EXTINF:4.0,\n/dash/v-2.m4s\n"
                        b"#EXT-X-ENDLIST\n",
                        "application/vnd.apple.mpegurl",
                    )
                if path == "/hls/audio.m3u8":
                    return self.send_body(
                        b"#EXTM3U\n#EXT-X-TARGETDURATION:12\n#EXT-X-MEDIA-SEQUENCE:0\n"
                        b'#EXT-X-MAP:URI="/dash/a-init.mp4"\n'
                        b"#EXTINF:12.0,\n/dash/a-0.m4s\n"
                        b"#EXT-X-ENDLIST\n",
                        "application/vnd.apple.mpegurl",
                    )
                target = f"http://127.0.0.1:{proxy.upstream_port}{path}"
                if parsed.query:
                    target += "?" + parsed.query
                try:
                    with urlopen(Request(target), timeout=30) as response:
                        body = response.read()
                        status = response.status
                        content_type = response.headers.get("Content-Type", "application/octet-stream")
                except Exception as error:
                    status = getattr(error, "code", 502)
                    reader = getattr(error, "read", None)
                    raw = reader() if callable(reader) else None
                    body = raw if isinstance(raw, bytes) else str(error).encode()
                    content_type = "text/plain"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self):
        with self.lock:
            self.counts.clear()

    def copy_counts(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

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


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        matches = [job for job in read_jobs(db) if job.get("source") == source]
        if matches:
            return matches[-1]
        last = matches
        time.sleep(0.1)
    raise RuntimeError(f"HLS capture did not create a job: {last}")


def wait_job(db: str, job_id: str, predicate, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in read_jobs(db) if job.get("id") == job_id), None)
        if last is not None and predicate(last):
            return last
        time.sleep(0.1)
    raise RuntimeError(f"job {job_id} did not reach expected state: {last}")


def make_reference(root: str, fragments: dict[str, bytes]) -> tuple[str, str]:
    video = os.path.join(root, "hls-reference-video.track")
    audio = os.path.join(root, "hls-reference-audio.track")
    output = os.path.join(root, "hls-reference.mp4")
    with open(video, "wb") as handle:
        for path in SEGMENTS[:4]:
            handle.write(fragments[path])
    with open(audio, "wb") as handle:
        for path in SEGMENTS[4:]:
            handle.write(fragments[path])
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video, "-i", audio,
         "-map", "0:0", "-map", "1:0", "-c", "copy", output],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
    return output, hashlib.sha256(open(output, "rb").read()).hexdigest()


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
    root = tempfile.mkdtemp(prefix="dm-hls-fmp4-")
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
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        support.wait_http(f"http://127.0.0.1:{upstream_port}/dash/manifest.mpd")
        proxy = HlsProxy(upstream_port)
        proxy_base = f"http://127.0.0.1:{proxy.port}"
        source = f"{proxy_base}/hls/master.m3u8"
        fragments = {}
        for path in SEGMENTS:
            with urlopen(f"{proxy_base}{path}", timeout=30) as response:
                fragments[path] = response.read()
        _reference, reference_hash = make_reference(root, fragments)
        proxy.reset()

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
        support.invoke(
            client,
            "create_provisional",
            {"input": {"source": source, "name": "hls-fmp4.mp4", "media": True, "maxConnections": 2}},
        )
        created = wait_source_job(db_path(root), source)
        job_id = created["id"]
        ready = wait_job(
            db_path(root), job_id,
            lambda job: job.get("state") == "finalizing" and job.get("segments", {}).get("completed") == 6,
        )
        destination = os.path.join(root, "Downloads", "hls-fmp4.mp4")
        support.invoke(
            client,
            "commit_provisional",
            {"id": job_id, "input": {"name": "hls-fmp4.mp4", "destination": destination, "maxConnections": 2}},
        )
        completed = wait_job(db_path(root), job_id, lambda job: job.get("state") == "completed")
        client.close()
        client = None
        output = completed["destination"]
        output_hash = hashlib.sha256(open(output, "rb").read()).hexdigest()
        assert output_hash == reference_hash, (output_hash, reference_hash)
        counts = proxy.copy_counts()
        assert counts.get("/hls/master.m3u8") == 1, counts
        assert counts.get("/hls/video.m3u8") == 1, counts
        assert counts.get("/hls/audio.m3u8") == 1, counts
        for path in SEGMENTS:
            assert counts.get(path) == 1, counts
        print(
            f"HLS-FMP4: PASS (ready={ready['segments']['completed']}/6, tracks=video+audio, output={len(open(output, 'rb').read())} bytes, sha256={output_hash})",
            flush=True,
        )
        print(f"HLS-REQUESTS: {json.dumps(counts, sort_keys=True)}", flush=True)
        print(f"E2E-ROOT: {root}", flush=True)
        print("HLS-FMP4-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "HLS app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "HLS fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "HLS Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"HLS-FMP4-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
