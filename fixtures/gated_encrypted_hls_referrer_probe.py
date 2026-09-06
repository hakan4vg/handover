#!/usr/bin/env python3
"""Resident proof: Referer replay composes with AES-128 HLS VOD.

The local server gates the playlist, key, init fragment, and encrypted fMP4
segments on the captured page Referer. The plaintext fragments come from the
existing generic fMP4 fixture; only the transport representation is encrypted
at probe startup with RFC 8216 AES-128-CBC. Phase A proves the current binary
fails honestly without page context. Phase B uses pageUrl through the real
Inspector command path and compares the committed output with an independent
plaintext reference muxed through the resident's exact ffmpeg invocation.
"""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import segmented_restart_probe as support
import hls_fmp4_probe as hls

BIN = support.BIN
MEDIA_DIR = Path(__file__).resolve().parent / "media"
SEQUENCE = 7
SEGMENT_NAMES = ["v-0.m4s", "v-1.m4s", "v-2.m4s"]
KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/149.0.0.0 Safari/537.36"


def read_jobs(db: str) -> list[dict]:
    if not os.path.exists(db):
        return []
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT payload FROM jobs").fetchall()
    return [json.loads(payload) for (payload,) in rows]


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in read_jobs(db) if job.get("source") == source]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"no job for {source}: {last}")


def wait_job(db: str, job_id: str, predicate, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in read_jobs(db) if job.get("id") == job_id), None)
        if last is not None and predicate(last):
            return last
        time.sleep(0.2)
    raise RuntimeError(f"job {job_id} did not reach expected state: {last}")


def encrypt_segment(plain: bytes, sequence: int) -> bytes:
    iv = sequence.to_bytes(16, "big")
    result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", KEY.hex(), "-iv", iv.hex()],
        input=plain,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


def reference_output(root: Path, plaintext: list[bytes]) -> tuple[Path, str]:
    track = root / "reference.track"
    output = root / "reference.mp4"
    track.write_bytes(Path(MEDIA_DIR / "v-init.mp4").read_bytes() + b"".join(plaintext))
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(track),
         "-map", "0", "-c", "copy", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


class GatedEncryptedHls:
    def __init__(self, require_browser_user_agent: bool = False) -> None:
        self.require_browser_user_agent = require_browser_user_agent
        self.plain = [Path(MEDIA_DIR / name).read_bytes() for name in SEGMENT_NAMES]
        self.encrypted = [encrypt_segment(body, SEQUENCE + index) for index, body in enumerate(self.plain)]
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()
        owner = self
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler(owner))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _handler(owner: "GatedEncryptedHls"):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send_body(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def gated(self) -> bool:
                if self.headers.get("Referer") != owner.page_url:
                    return False
                return not owner.require_browser_user_agent or self.headers.get("User-Agent", "").startswith("Mozilla/5.0")

            def do_GET(self):
                path = urlsplit(self.path).path
                with owner.lock:
                    owner.counts[path] = owner.counts.get(path, 0) + 1
                if path == "/page/encrypted.html":
                    page = b"""<!doctype html>
<html><body><video id='player' controls autoplay playsinline></video>
<script src='https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js'></script>
<script>
const video = document.getElementById('player');
if (window.Hls && Hls.isSupported()) {
  const hls = new Hls();
  hls.loadSource('/hls/encrypted.m3u8');
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
} else { video.src = '/hls/encrypted.m3u8'; video.play(); }
</script></body></html>"""
                    return self.send_body(200, page, "text/html; charset=utf-8")
                if path in {"/hls/encrypted.m3u8", "/hls/key.bin", "/hls/init.mp4"} or path.startswith("/hls/v-"):
                    if not self.gated():
                        return self.send_body(403, b"referer required", "text/plain")
                if path == "/hls/encrypted.m3u8":
                    playlist = (
                        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
                        f"#EXT-X-MEDIA-SEQUENCE:{SEQUENCE}\n"
                        '#EXT-X-MAP:URI="/hls/init.mp4"\n'
                        '#EXT-X-KEY:METHOD=AES-128,URI="/hls/key.bin"\n'
                        + "".join(f"#EXTINF:4.0,\n/hls/{name}\n" for name in SEGMENT_NAMES)
                        + "#EXT-X-ENDLIST\n"
                    ).encode()
                    return self.send_body(200, playlist, "application/vnd.apple.mpegurl")
                if path == "/hls/key.bin":
                    return self.send_body(200, KEY, "application/octet-stream")
                if path == "/hls/init.mp4":
                    return self.send_body(200, Path(MEDIA_DIR / "v-init.mp4").read_bytes(), "video/mp4")
                if path.startswith("/hls/v-") and path.endswith(".m4s"):
                    try:
                        index = SEGMENT_NAMES.index(path.rsplit("/", 1)[1])
                    except ValueError:
                        return self.send_body(404, b"missing fixture", "text/plain")
                    return self.send_body(200, owner.encrypted[index], "video/iso.segment")
                return self.send_body(404, b"missing fixture", "text/plain")

        return Handler

    @property
    def page_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/page/encrypted.html"

    @property
    def source(self) -> str:
        return f"http://127.0.0.1:{self.port}/hls/encrypted.m3u8"

    def snapshot_counts(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def fetch_reference(server: GatedEncryptedHls, root: Path) -> tuple[Path, str]:
    headers = {"Referer": server.page_url, "User-Agent": BROWSER_UA}
    for path in ["/hls/encrypted.m3u8", "/hls/key.bin", "/hls/init.mp4", *[f"/hls/{name}" for name in SEGMENT_NAMES]]:
        with urlopen(Request(f"http://127.0.0.1:{server.port}{path}", headers=headers), timeout=30) as response:
            if path == "/hls/encrypted.m3u8":
                assert b"#EXT-X-KEY:METHOD=AES-128" in response.read()
            else:
                response.read()
    return reference_output(root, server.plain)


def main() -> int:
    root_path = Path(tempfile.mkdtemp(prefix="dm-gated-encrypted-hls-"))
    xvfb = server = app = client = None
    try:
        server = GatedEncryptedHls()
        reference, reference_hash = fetch_reference(server, root_path)
        print(f"ENCRYPTED-HLS-REFERENCE: segments=4 bytes={reference.stat().st_size} sha256={reference_hash}", flush=True)
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        inspector_port = support.free_port()
        app_log_path = root_path / "app.log"
        with app_log_path.open("wb") as app_log:
            app = subprocess.Popen([BIN], env=support.app_env(str(root_path), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
        client = support.wait_inspector(inspector_port)
        support.wait_tauri(client)
        db = support.db_path(str(root_path))

        support.invoke(client, "create_provisional", {"input": {"source": server.source, "name": "encrypted-red.mp4", "media": True}})
        red_id = wait_source_job(db, server.source)["id"]
        red = wait_job(db, red_id, lambda job: job.get("state") in ("failed", "completed"))
        print("ENCRYPTED-HLS-RED:", json.dumps({"state": red.get("state"), "error": red.get("error")}, sort_keys=True), flush=True)
        assert red.get("state") == "failed" and "403" in str(red.get("error")), red

        support.invoke(client, "create_provisional", {"input": {"source": server.source, "name": "encrypted-green.mp4", "media": True, "pageUrl": server.page_url, "maxConnections": 2}})
        green_id = [job for job in read_jobs(db) if job.get("source") == server.source and job.get("id") != red_id][-1]["id"]
        ready = wait_job(db, green_id, lambda job: job.get("state") == "finalizing" and (job.get("segments") or {}).get("completed") == 4)
        destination = root_path / "Downloads" / "encrypted-green.mp4"
        support.invoke(client, "commit_provisional", {"id": green_id, "input": {"name": "encrypted-green.mp4", "destination": str(destination), "maxConnections": 2}})
        done = wait_job(db, green_id, lambda job: job.get("state") == "completed")
        output = destination.read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert done.get("provisional") is False
        assert len(output) == reference.stat().st_size and output_hash == reference_hash, (len(output), output_hash)
        print(f"ENCRYPTED-HLS-READY: {json.dumps({'segments': ready.get('segments')}, sort_keys=True)}", flush=True)
        print(f"ENCRYPTED-HLS: PASS (output_bytes={len(output)}, output_sha256={output_hash}, counts={json.dumps(server.snapshot_counts(), sort_keys=True)})", flush=True)
        print("GATED-ENCRYPTED-HLS-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app is not None and app.poll() is not None:
            log_path = root_path / "app.log"
            if log_path.exists():
                print(f"APP-LOG: {log_path.read_text(encoding='utf-8', errors='replace')[-2500:]}", flush=True)
        raise
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "encrypted HLS app")
        if server is not None:
            server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "encrypted HLS Xvfb")
        import shutil
        shutil.rmtree(root_path, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"GATED-ENCRYPTED-HLS-PROBE: FAIL: {error}", flush=True)
        raise
