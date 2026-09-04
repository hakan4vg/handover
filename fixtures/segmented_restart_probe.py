#!/usr/bin/env python3
"""Real-binary segmented restart/recovery proof (no GUI input, no XTEST).

This probe fronts the existing finite multi-track DASH fixture with a local
counting/delaying proxy. It seeds a committed job with three real fragments,
lets the application persist more fragments, kills only that disposable app,
then verifies restart reuse, cancellation, manual retry, and bounded fragment
retries. The final MP4 is compared byte-for-byte with an independent FFmpeg
assembly using the same six fixture fragments.

The fixture server remains the source of every manifest and media byte:
  fixtures/server.py --port <upstream>
  /dash/manifest.mpd
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BIN = "/srv/repos/downloadmanager/src-tauri/target/debug/download-manager"
FIXTURE_SERVER = os.path.join(os.path.dirname(__file__), "server.py")
DISPLAY = os.environ.get("DM_DISPLAY")
SEGMENTS = [
    ("video", "/dash/v-init.mp4"),
    ("video", "/dash/v-0.m4s"),
    ("video", "/dash/v-1.m4s"),
    ("video", "/dash/v-2.m4s"),
    ("audio", "/dash/a-init.mp4"),
    ("audio", "/dash/a-0.m4s"),
]
SEED_SEGMENTS = [
    (0, "/dash/v-init.mp4"),
    (1, "/dash/v-0.m4s"),
    (4, "/dash/a-init.mp4"),
]
JOB_ID = "segmented-restart-recovery"
RETRY_JOB_ID = "segmented-bounded-retry"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as error:  # startup race only
            last_error = error
        time.sleep(0.1)
    raise RuntimeError(f"fixture did not become ready: {url}: {last_error}")


class DashProxy:
    """Count and optionally delay/fail requests while forwarding fixture bytes."""

    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.delay = 0.0
        self.fail_path: str | None = None
        self.fail_remaining = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                with proxy.lock:
                    proxy.counts[path] += 1
                    should_fail = path == proxy.fail_path and proxy.fail_remaining > 0
                    if should_fail:
                        proxy.fail_remaining -= 1
                    delay = proxy.delay
                if should_fail:
                    body = b"injected retryable fixture failure"
                    self.send_response(503)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path.endswith((".mp4", ".m4s")) and path.startswith("/dash/"):
                    time.sleep(delay)
                upstream_path = path
                if upstream_path.startswith("/dash/retry/"):
                    upstream_path = "/dash/" + upstream_path.removeprefix("/dash/retry/")
                target = f"http://127.0.0.1:{proxy.upstream_port}{upstream_path}"
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
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(data)
                    except BrokenPipeError:
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset_counts(self) -> None:
        with self.lock:
            self.counts.clear()

    def count(self, path: str) -> int:
        with self.lock:
            return self.counts[path]

    def counts_copy(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def fnv_segment_identity(proxy_base: str) -> str:
    value = 0xCBF29CE484222325
    tracks = [
        ("video", [path for kind, path in SEGMENTS if kind == "video"]),
        ("audio", [path for kind, path in SEGMENTS if kind == "audio"]),
    ]
    for kind, paths in tracks:
        for byte in kind.encode() + bytes([0xFF]):
            value ^= byte
            value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        for path in paths:
            url = f"{proxy_base}{path}"
            for byte in url.encode() + bytes([0xFE]):
                value ^= byte
                value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def app_env(home: str, inspector_port: int | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=home,
        XDG_CACHE_HOME=os.path.join(home, ".cache"),
        DISPLAY=DISPLAY or ":99",
        WEBKIT_DISABLE_COMPOSITING_MODE="1",
    )
    if inspector_port is not None:
        env["WEBKIT_INSPECTOR_HTTP_SERVER"] = f"127.0.0.1:{inspector_port}"
    else:
        env.pop("WEBKIT_INSPECTOR_HTTP_SERVER", None)
    return env


def db_path(home: str) -> str:
    return os.path.join(home, ".local", "share", "com.downloadmanager.app", "download-manager.db")


def wait_db(home: str, timeout: float = 30.0) -> str:
    path = db_path(home)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return path
        time.sleep(0.1)
    raise RuntimeError(f"database did not appear: {path}")


def terminate_only(proc: subprocess.Popen[bytes], label: str) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print(f"{label}: SIGTERM did not exit; sending SIGKILL", flush=True)
        proc.kill()
        proc.wait(timeout=10)


def boot_once(home: str) -> None:
    proc = subprocess.Popen(
        [BIN], env=app_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_db(home)
    finally:
        terminate_only(proc, "initial app")


def read_jobs(db: str) -> list[dict]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT payload FROM jobs").fetchall()
    return [json.loads(payload) for (payload,) in rows]


def job_by_id(db: str, job_id: str) -> dict | None:
    return next((job for job in read_jobs(db) if job.get("id") == job_id), None)


def wait_job(db: str, job_id: str, predicate, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = job_by_id(db, job_id)
        if last is not None and predicate(last):
            return last
        time.sleep(0.05)
    raise RuntimeError(f"job {job_id} did not reach expected state: {last}")


def load_settings(db: str) -> dict:
    with sqlite3.connect(db) as connection:
        (payload,) = connection.execute(
            "SELECT payload FROM settings WHERE id = 1"
        ).fetchone()
    return json.loads(payload)


def write_settings(db: str, settings: dict) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE settings SET payload = ? WHERE id = 1", (json.dumps(settings),)
        )
        connection.commit()


def seed_job(
    db: str,
    home: str,
    proxy_base: str,
    job_id: str,
    name: str,
    fragment_bytes: dict[str, bytes],
) -> str:
    temp = os.path.join(home, ".cache", "download-manager", "tmp", f"{job_id}.part")
    segment_dir = Path(temp + ".segments")
    for track, path in SEED_SEGMENTS:
        index = next(index for index, (_kind, item) in enumerate(SEGMENTS) if item == path)
        track_number = 0 if track < 4 else 1
        segment_path = segment_dir / f"{track_number:02}" / f"{index if track_number == 0 else index - 4:08}.part"
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        segment_path.write_bytes(fragment_bytes[path])
    total_bytes = sum(len(fragment_bytes[path]) for _kind, path in SEGMENTS)
    seeded_bytes = sum(len(fragment_bytes[path]) for _track, path in SEED_SEGMENTS)
    job = {
        "id": job_id,
        "name": name,
        "source": f"{proxy_base}/dash/manifest.mpd",
        "domain": "127.0.0.1",
        "kind": "video",
        "state": "downloading",
        "progress": 50.0,
        "downloaded": seeded_bytes,
        "total": total_bytes,
        "speed": 0,
        "eta": "Resuming",
        "connections": 2,
        "maxConnections": 2,
        "bandwidthLimit": None,
        "mode": "segments",
        "media": True,
        "mediaDetails": None,
        "mediaTracks": 2,
        "destination": os.path.join(home, "Downloads", name),
        "tempPath": temp,
        "resumable": True,
        "mime": "video/mp4",
        "error": None,
        "created": "Just now",
        "started": "Just now",
        "completed": None,
        "provisional": False,
        "segments": {"completed": 3, "total": 6, "identity": fnv_segment_identity(proxy_base)},
        "completedRanges": [],
        "resourceIdentity": None,
        "events": [],
    }
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO jobs (id, created_at, payload) VALUES (?, ?, ?)",
            (job_id, "Just now", json.dumps(job)),
        )
        connection.commit()
    return temp


def make_reference(root: str, fragments: dict[str, bytes]) -> tuple[str, str]:
    video_path = os.path.join(root, "reference-video.track")
    audio_path = os.path.join(root, "reference-audio.track")
    output_path = os.path.join(root, "reference.mp4")
    with open(video_path, "wb") as video:
        for path in ["/dash/v-init.mp4", "/dash/v-0.m4s", "/dash/v-1.m4s", "/dash/v-2.m4s"]:
            video.write(fragments[path])
    with open(audio_path, "wb") as audio:
        for path in ["/dash/a-init.mp4", "/dash/a-0.m4s"]:
            audio.write(fragments[path])
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_path, "-i", audio_path,
            "-map", "0:0", "-map", "1:0", "-c", "copy", output_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
    return output_path, hashlib.sha256(Path(output_path).read_bytes()).hexdigest()


class WebKitClient:
    """Minimal WebKit Inspector Protocol client for direct Tauri invoke calls."""

    def __init__(self, port: int):
        import base64
        import struct

        self.base64 = base64
        self.struct = struct
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        key = base64.b64encode(b"0123456789abcdef").decode()
        self.sock.sendall(
            (
                f"GET /socket/1/1/WebPage HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        header = b""
        while b"\r\n\r\n" not in header:
            header += self.sock.recv(4096)
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebKit inspector did not upgrade: {header!r}")
        self.next_id = 0
        self.target_id = None
        self._outer("Target.setPauseOnStart", {"pauseOnStart": False})
        if self.target_id is None:
            try:
                targets = self._outer("Target.getTargets")
            except RuntimeError as error:
                raise RuntimeError(f"no WebKit page target event: {error}") from error
            for info in targets.get("targetInfos", []):
                if info.get("type") == "page":
                    self.target_id = info.get("targetId")
                    break
        if not self.target_id:
            raise RuntimeError("no WebKit page target")
        self._nested("Runtime.enable")

    def _recv_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise RuntimeError("WebKit inspector closed")
            data += chunk
        return data

    def _recv(self) -> str:
        first, second = self._recv_exact(2)
        length = second & 0x7F
        if length == 126:
            length = self.struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = self.struct.unpack(">Q", self._recv_exact(8))[0]
        if second & 0x80:
            self._recv_exact(4)
        return self._recv_exact(length).decode()

    def _send(self, text: str) -> None:
        raw = text.encode()
        mask = b"abcd"
        length = len(raw)
        if length <= 125:
            header = bytes([0x81, 0x80 | length])
        elif length <= 65535:
            header = bytes([0x81, 0x80 | 126]) + self.struct.pack(">H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + self.struct.pack(">Q", length)
        self.sock.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw)))

    def _outer(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        ident = self.next_id
        self._send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._recv())
            if message.get("method") == "Target.targetCreated":
                info = message.get("params", {}).get("targetInfo", {})
                if info.get("type") == "page" and self.target_id is None:
                    self.target_id = info.get("targetId")
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def _nested(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        inner_id = self.next_id + 1000
        self._outer(
            "Target.sendMessageToTarget",
            {"targetId": self.target_id, "message": json.dumps({"id": inner_id, "method": method, "params": params or {}})},
        )
        while True:
            message = json.loads(self._recv())
            if message.get("method") != "Target.dispatchMessageFromTarget":
                continue
            nested = json.loads(message.get("params", {}).get("message", "{}"))
            if nested.get("id") == inner_id:
                if "error" in nested:
                    raise RuntimeError(nested["error"])
                return nested.get("result", {})

    def evaluate(self, expression: str):
        result = self._nested(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"WebKit evaluate exception: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.sock.close()


def wait_inspector(port: int, timeout: float = 30.0) -> WebKitClient:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return WebKitClient(port)
        except Exception as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"WebKit inspector did not become ready: {last_error}")


def wait_tauri(client: WebKitClient, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = client.evaluate("typeof window.__TAURI_INTERNALS__")
            if last == "object":
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Tauri bridge did not become ready: {last}")


def invoke(client: WebKitClient, command: str, payload: dict) -> None:
    expression = (
        "(window.__TAURI_INTERNALS__.invoke("
        f"{json.dumps(command)}, {json.dumps(payload)}), 1)"
    )
    result = client.evaluate(expression)
    if result != 1:
        raise RuntimeError(f"Tauri invoke dispatch failed: {command}: {result}")


def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if not os.path.exists(BIN):
        raise RuntimeError(f"binary missing: {BIN}")
    root = os.environ.get("DM_HOME") or tempfile.mkdtemp(prefix="dm-segmented-restart-")
    if os.environ.get("DM_HOME"):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    upstream_port = free_port()
    upstream = subprocess.Popen(
        [sys.executable, FIXTURE_SERVER, "--port", str(upstream_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proxy = None
    app = None
    client = None
    xvfb = None
    try:
        global DISPLAY
        if DISPLAY is None:
            for number in range(110, 200):
                socket_path = f"/tmp/.X11-unix/X{number}"
                if os.path.exists(socket_path):
                    continue
                DISPLAY = f":{number}"
                xvfb = subprocess.Popen(
                    ["Xvfb", DISPLAY, "-screen", "0", "1280x900x24", "-ac", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(100):
                    if xvfb.poll() is not None:
                        raise RuntimeError(f"Xvfb exited with {xvfb.returncode}")
                    if os.path.exists(socket_path):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(f"Xvfb did not become ready: {DISPLAY}")
                break
            if xvfb is None:
                raise RuntimeError("no disposable Xvfb display was available")
        wait_http(f"http://127.0.0.1:{upstream_port}/dash/manifest.mpd")
        proxy = DashProxy(upstream_port)
        proxy_base = f"http://127.0.0.1:{proxy.port}"
        manifest_url = f"{proxy_base}/dash/manifest.mpd"
        fragments = {}
        for _kind, path in SEGMENTS:
            with urlopen(f"{proxy_base}{path}", timeout=30) as response:
                fragments[path] = response.read()
        reference, reference_hash = make_reference(root, fragments)

        boot_once(root)
        db = wait_db(root)
        settings = load_settings(db)
        settings.update({"maxConnections": 2, "retryAutomatically": True, "maxRetries": 2})
        write_settings(db, settings)
        first_temp = seed_job(db, root, proxy_base, JOB_ID, "segmented-recovery.mp4", fragments)
        proxy.reset_counts()

        proxy.delay = 5.0
        app = subprocess.Popen(
            [BIN], env=app_env(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        phase1 = wait_job(db, JOB_ID, lambda job: job.get("segments", {}).get("completed", 0) >= 5 and job.get("state") == "downloading", 45)
        first_counts = proxy.counts_copy()
        assert phase1["segments"]["completed"] == 5, phase1
        assert first_counts.get("/dash/v-init.mp4", 0) == 0
        assert first_counts.get("/dash/v-0.m4s", 0) == 0
        assert first_counts.get("/dash/a-init.mp4", 0) == 0
        assert first_counts.get("/dash/v-1.m4s", 0) == 1
        assert first_counts.get("/dash/v-2.m4s", 0) == 1
        assert first_counts.get("/dash/a-0.m4s", 0) == 1
        app.kill()
        app.wait(timeout=10)
        app = None
        persisted = job_by_id(db, JOB_ID)
        assert persisted and persisted["state"] == "downloading", persisted
        assert persisted["segments"]["completed"] == 5, persisted
        assert os.path.exists(first_temp + ".segments/00/00000002.part")
        assert not os.path.exists(first_temp + ".segments/01/00000001.part")
        print(
            f"PHASE-1-PERSIST: PASS (committed job durably recorded {persisted['segments']['completed']}/6 fragments; seeded paths were not fetched)",
            flush=True,
        )

        inspector_port = free_port()
        proxy.delay = 10.0
        proxy.reset_counts()
        phase2_log = open(os.path.join(root, "phase2-app.log"), "wb")
        app = subprocess.Popen(
            [BIN], env=app_env(root, inspector_port), stdout=phase2_log, stderr=subprocess.STDOUT
        )
        phase2_log.close()
        try:
            client = wait_inspector(inspector_port)
            wait_tauri(client)
        except Exception:
            print(f"PHASE2-APP: poll={app.poll()}", flush=True)
            log_path = Path(root) / "phase2-app.log"
            if log_path.exists():
                print(f"PHASE2-APP-LOG: {log_path.read_text(errors='replace')}", flush=True)
            raise
        phase2 = wait_job(db, JOB_ID, lambda job: job.get("segments", {}).get("completed") == 5 and proxy.count("/dash/a-0.m4s") >= 1, 45)
        before_cancel = proxy.count("/dash/a-0.m4s")
        invoke(client, "cancel_job", {"id": JOB_ID})
        cancelled = wait_job(db, JOB_ID, lambda job: job.get("state") == "failed", 15)
        time.sleep(1.0)
        after_cancel = proxy.count("/dash/a-0.m4s")
        assert after_cancel == before_cancel, (before_cancel, after_cancel)
        assert cancelled.get("connections") == 0, cancelled
        print(
            f"CANCEL-BOUND: PASS (state=failed, connections=0, a-0 requests stayed {after_cancel} after cancel)",
            flush=True,
        )
        proxy.delay = 0.05
        invoke(client, "retry_job", {"id": JOB_ID})
        completed = wait_job(db, JOB_ID, lambda job: job.get("state") == "completed", 60)
        client.close()
        client = None
        output = completed["destination"]
        assert os.path.exists(output), output
        assert sha256(output) == reference_hash, (sha256(output), reference_hash)
        assert proxy.count("/dash/v-init.mp4") == 0
        assert proxy.count("/dash/v-0.m4s") == 0
        assert proxy.count("/dash/a-init.mp4") == 0
        assert proxy.count("/dash/v-1.m4s") == 0
        assert proxy.count("/dash/v-2.m4s") == 0
        assert proxy.count("/dash/a-0.m4s") == 2, proxy.counts_copy()
        print(
            f"RESTART-REUSE: PASS (5 persisted fragments reused; retry fetched only a-0; output {len(Path(output).read_bytes())} bytes, sha256={sha256(output)[:16]}…)",
            flush=True,
        )

        terminate_only(app, "phase-2 app")
        app = None
        retry_temp = seed_job(db, root, proxy_base, RETRY_JOB_ID, "segmented-bounded-retry.mp4", fragments)
        proxy.reset_counts()
        proxy.delay = 0.05
        proxy.fail_path = "/dash/a-0.m4s"
        proxy.fail_remaining = 3
        inspector_port = free_port()
        retry_log = open(os.path.join(root, "retry-app.log"), "wb")
        app = subprocess.Popen(
            [BIN], env=app_env(root, inspector_port), stdout=retry_log, stderr=subprocess.STDOUT
        )
        retry_log.close()
        try:
            client = wait_inspector(inspector_port)
            wait_tauri(client)
        except Exception:
            print(f"RETRY-APP: poll={app.poll()}", flush=True)
            log_path = Path(root) / "retry-app.log"
            if log_path.exists():
                print(f"RETRY-APP-LOG: {log_path.read_text(errors='replace')}", flush=True)
            raise
        failed_retry = wait_job(db, RETRY_JOB_ID, lambda job: job.get("state") == "failed", 60)
        failed_count = proxy.count("/dash/a-0.m4s")
        time.sleep(1.0)
        assert failed_count == 3, proxy.counts_copy()
        assert proxy.count("/dash/a-0.m4s") == failed_count
        assert failed_retry.get("error") and "503" in failed_retry["error"], failed_retry
        assert os.path.exists(retry_temp + ".segments/01/00000000.part")
        print(
            f"RETRY-BOUND: PASS (fragment returned bounded 3 attempts for maxRetries=2; completed={failed_retry['segments']['completed']}/6; no retry storm)",
            flush=True,
        )
        proxy.fail_remaining = 0
        proxy.fail_path = None
        invoke(client, "retry_job", {"id": RETRY_JOB_ID})
        retried = wait_job(db, RETRY_JOB_ID, lambda job: job.get("state") == "completed", 60)
        client.close()
        client = None
        retry_output = retried["destination"]
        assert sha256(retry_output) == reference_hash, (sha256(retry_output), reference_hash)
        assert proxy.count("/dash/v-init.mp4") == 0
        assert proxy.count("/dash/v-0.m4s") == 0
        assert proxy.count("/dash/a-init.mp4") == 0
        assert proxy.count("/dash/v-1.m4s") == 1
        assert proxy.count("/dash/v-2.m4s") == 1
        assert proxy.count("/dash/a-0.m4s") == 4, proxy.counts_copy()
        print(
            f"RETRY-RECOVERY: PASS (manual retry reused 5 fragments; final sha256={sha256(retry_output)[:16]}…)",
            flush=True,
        )
        print(f"REFERENCE: {reference} sha256={reference_hash}", flush=True)
        print(f"E2E-ROOT: {root}", flush=True)
        print("SEGMENTED-RESTART-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            terminate_only(app, "cleanup app")
        if proxy is not None:
            proxy.stop()
        terminate_only(upstream, "fixture server")
        if xvfb is not None:
            terminate_only(xvfb, "Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SEGMENTED-RESTART-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
