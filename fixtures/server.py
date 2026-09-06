#!/usr/bin/env python3
"""Deterministic download-engine fixture server (SPEC §18).

Stdlib only. Serves the shapes the transfer engine must handle:
  range-capable file / range-ignoring file / slow-drip file
  redirects (single + chain) / token auth / referer-gated file / one-use URL / changed identity
  finite HLS (TS) / HLS master+variant / live HLS (must be rejected)
  static DASH SegmentList with separate audio+video / SegmentTemplate /
  adaptation-level SegmentList / dynamic DASH (must be rejected) / progressive media
  retryable 503 / plain 404

All payload bytes are deterministic (seeded PRNG stream), so resume and
identity checks are reproducible.

Usage:  python3 server.py [--port 8901]
"""

import argparse
import os
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SEG_TS_COUNT = 6
HLS_SLOW_COUNT = 4
HLS_SLOW_BYTES = 1024 * 1024
DASH_V_SEGS = 3
DASH_A_SEGS = 1
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")


def media_file(name: str) -> bytes | None:
    try:
        with open(os.path.join(MEDIA_DIR, name), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def file_data(name: str, seed: int, size: int) -> bytes:
    # Pre-generated once per (name, seed); slicing is O(1) C-speed. The old
    # per-byte generator was O(offset) Python and serialized all concurrent
    # range requests under the GIL, masquerading as an engine stall.
    key = (name, seed)
    if key not in FILE_DATA:
        FILE_DATA[key] = random.Random(seed).randbytes(size)
    return FILE_DATA[key]


FILE_DATA: dict = {}


FILES = {
    # name: (size, seed, range_support)
    "range.bin": (8 * 1024 * 1024, 0x11, True),
    "no-range.bin": (2 * 1024 * 1024, 0x22, False),
    "slow.bin": (512 * 1024, 0x33, True),
    "token.bin": (1 * 1024 * 1024, 0x44, True),
    "changed.bin": (256 * 1024, 0x55, True),
    "sample.mp4": (1 * 1024 * 1024, 0x66, True),
    "ref-gated.bin": (1 * 1024 * 1024, 0x88, True),
}

ONE_USE = {}  # token -> bytes
ONE_USE_LOCK = threading.Lock()
ONE_USE_COUNTER = [0]


def parse_range(header: str, total: int):
    m = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
    if not m:
        return None
    start_s, end_s = m.groups()
    if start_s == "" and end_s == "":
        return None
    if start_s == "":  # suffix range
        length = int(end_s)
        if length <= 0:
            return None
        return (max(0, total - length), total - 1)
    start = int(start_s)
    end = int(end_s) if end_s != "" else total - 1
    if start >= total:
        return None
    return (start, min(end, total - 1))


class Handler(BaseHTTPRequestHandler):
    server_version = "DMFixtures/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def _send_bytes(self, data: bytes, code=200, headers=None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_file(self, name: str, seed: int, total: int, ranged: bool, extra=None):
        headers = dict(extra or {})
        range_header = self.headers.get("Range")
        if ranged and range_header:
            parsed = parse_range(range_header, total)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            start, end = parsed
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Accept-Ranges"] = "bytes"
            self._send_bytes(file_data(name, seed, total)[start:end + 1], 206, headers)
            return
        if ranged:
            headers["Accept-Ranges"] = "bytes"
        # no-range files deliberately omit Accept-Ranges and ignore Range.
        if not ranged and range_header:
            pass
        self._send_bytes(file_data(name, seed, total), 200, headers)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        if path == "/file/range.bin":
            size, seed, _ = FILES["range.bin"]
            return self._serve_file("range.bin", seed, size, True)
        if path == "/file/no-range.bin":
            size, seed, _ = FILES["no-range.bin"]
            data = file_data("no-range.bin", seed, size)
            return self._send_bytes(data if self.command == "GET" else b"", 200)
        if path == "/file/slow.bin":
            size, seed, _ = FILES["slow.bin"]
            # Slow drip: 32 KiB chunks with small pauses (tests cancellation).
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command == "HEAD":
                return
            import time
            for off in range(0, size, 32 * 1024):
                try:
                    self.wfile.write(file_data("slow.bin", seed, size)[off:off + 32 * 1024])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.02)
            return
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file/range.bin")
            self.end_headers()
            return
        if path == "/redirect-chain":
            self.send_response(302)
            self.send_header("Location", "/redirect")
            self.end_headers()
            return
        if path == "/auth/token.bin":
            token = qs.get("token", [""])[0] or self.headers.get("X-Token", "")
            if token != "secret123":
                return self._send_bytes(b"forbidden", 403)
            size, seed, _ = FILES["token.bin"]
            return self._serve_file("token.bin", seed, size, True)
        if path == "/file/ref-gated.bin":
            # Hotlink-style gate: 403 unless the Referer parses and its host
            # matches this server's host (SPEC §5.1 request-context replay).
            referer = self.headers.get("Referer", "")
            host = (self.headers.get("Host", "") or "").split(":")[0].lower()
            try:
                ref_host = urlparse(referer).hostname or ""
            except Exception:
                ref_host = ""
            if not ref_host or ref_host.lower() != host:
                return self._send_bytes(b"referer required", 403)
            size, seed, _ = FILES["ref-gated.bin"]
            return self._serve_file("ref-gated.bin", seed, size, True)
        if path == "/file/origin-gated.bin":
            # Strict origin gate (SPEC §16): 403 unless the Referer is exactly
            # the embedding page's origin with no path or query. The probe
            # serves the page from 127.0.0.1 and this file is fetched cross-
            # origin (localhost), so only a stripped page-origin Referer
            # passes — a full page URL must fail.
            referer = self.headers.get("Referer", "")
            expected = f"http://127.0.0.1:{self.server.server_address[1]}"
            if referer != expected:
                return self._send_bytes(b"origin referer required", 403)
            size, seed, _ = FILES["ref-gated.bin"]
            return self._serve_file("ref-gated.bin", seed, size, True)
        if path == "/one-use/mint":
            with ONE_USE_LOCK:
                ONE_USE_COUNTER[0] += 1
                token = f"t{ONE_USE_COUNTER[0]}"
                ONE_USE[token] = file_data("one-use", 0x77, 64 * 1024)
            host = self.headers.get("Host", "127.0.0.1")
            return self._send_bytes(f"http://{host}/one-use/{token}".encode(), 200)
        m = re.match(r"^/one-use/(t\d+)$", path)
        if m:
            with ONE_USE_LOCK:
                data = ONE_USE.pop(m.group(1), None)
            if data is None:
                return self._send_bytes(b"consumed", 410)
            return self._send_bytes(data, 200, {"Accept-Ranges": "bytes"})
        if path == "/changed.bin":
            variant = qs.get("variant", ["1"])[0]
            seed = 0x55 if variant == "1" else 0x56
            size, _, _ = FILES["changed.bin"]
            return self._serve_file("changed.bin", seed, size, True, {"ETag": f'"v{variant}"'})
        if path == "/status/503":
            return self._send_bytes(b"unavailable", 503)
        # --- HLS ---
        if path == "/hls/master.m3u8":
            body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvod.m3u8\n"
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/vnd.apple.mpegurl"})
        if path == "/hls/ranged-vod.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
            for i in range(SEG_TS_COUNT):
                lines += ["#EXTINF:2.0,", f"seg{i}.ts"]
            lines.append("#EXT-X-ENDLIST")
            body = ("\n".join(lines) + "\n").encode()
            range_header = self.headers.get("Range")
            if range_header:
                parsed = parse_range(range_header, len(body))
                if parsed is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.end_headers()
                    return
                start, end = parsed
                return self._send_bytes(body[start:end + 1], 206, {"Content-Type": "application/vnd.apple.mpegurl", "Content-Range": f"bytes {start}-{end}/{len(body)}", "Accept-Ranges": "bytes"})
            return self._send_bytes(body, 200, {"Content-Type": "application/vnd.apple.mpegurl", "Accept-Ranges": "bytes"})
        if path == "/hls/vod.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
            for i in range(SEG_TS_COUNT):
                lines += ["#EXTINF:2.0,", f"seg{i}.ts"]
            lines.append("#EXT-X-ENDLIST")
            return self._send_bytes(("\n".join(lines) + "\n").encode(), 200, {"Content-Type": "application/vnd.apple.mpegurl"})
        if path == "/hls/slow.m3u8":
            lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:0"]
            for i in range(HLS_SLOW_COUNT):
                lines += ["#EXTINF:1.0,", f"slow{i}.bin"]
            lines.append("#EXT-X-ENDLIST")
            return self._send_bytes(("\n".join(lines) + "\n").encode(), 200, {"Content-Type": "application/vnd.apple.mpegurl"})
        if path == "/hls/live.m3u8":
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg0.ts\n"
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/vnd.apple.mpegurl"})
        m = re.match(r"^/hls/(seg(\d+)\.ts)$", path)
        if m:
            idx = int(m.group(2))
            return self._send_bytes(file_data(f"seg{idx}", 0x80 + idx, 188 * 16), 200, {"Content-Type": "video/mp2t"})
        m = re.match(r"^/hls/(slow(\d+)\.bin)$", path)
        if m:
            idx = int(m.group(2))
            if idx >= HLS_SLOW_COUNT:
                return self._send_bytes(b"missing fixture", 404)
            return self._send_bytes(file_data(f"slow{idx}", 0x180 + idx, HLS_SLOW_BYTES), 200, {"Content-Type": "application/octet-stream"})
        # --- DASH ---
        if path == "/dash/manifest.mpd":
            v = "".join(f'<SegmentURL media="v-{i}.m4s"/>' for i in range(DASH_V_SEGS))
            a = "".join(f'<SegmentURL media="a-{i}.m4s"/>' for i in range(DASH_A_SEGS))
            body = (
                '<MPD type="static" mediaPresentationDuration="PT12S"><Period>'
                '<AdaptationSet contentType="video"><Representation id="v">'
                f"<BaseURL>/dash/</BaseURL><SegmentList><Initialization sourceURL=\"v-init.mp4\"/>{v}</SegmentList>"
                "</Representation></AdaptationSet>"
                '<AdaptationSet contentType="audio"><Representation id="a">'
                f"<BaseURL>/dash/</BaseURL><SegmentList><Initialization sourceURL=\"a-init.mp4\"/>{a}</SegmentList>"
                "</Representation></AdaptationSet>"
                "</Period></MPD>"
            )
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/dash+xml"})
        if path == "/dash/adaptation-list.mpd":
            segments = "".join(f'<SegmentURL media="v-{i}.m4s"/>' for i in range(DASH_V_SEGS))
            body = (
                '<MPD type="static"><Period><AdaptationSet contentType="video">'
                '<BaseURL>/dash/</BaseURL><SegmentList><Initialization sourceURL="v-init.mp4"/>'
                f"{segments}</SegmentList><Representation id=\"video\"/>"
                '</AdaptationSet></Period></MPD>'
            )
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/dash+xml"})
        if path == "/dash/template.mpd":
            body = (
                '<MPD type="static" mediaPresentationDuration="PT6S"><Period>'
                '<AdaptationSet contentType="video"><Representation id="video">'
                '<BaseURL>/dash/</BaseURL>'
                '<SegmentTemplate timescale="1" media="t-$Number$.m4s" initialization="t-init.mp4" startNumber="1">'
                '<SegmentTimeline><S t="0" d="2" r="2"/></SegmentTimeline>'
                "</SegmentTemplate></Representation></AdaptationSet>"
                "</Period></MPD>"
            )
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/dash+xml"})
        if path == "/dash/live.mpd":
            body = '<MPD type="dynamic" minimumUpdatePeriod="PT2S"><Period><AdaptationSet><Representation><SegmentTemplate media="s.m4s"/></Representation></AdaptationSet></Period></MPD>'
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/dash+xml"})
        # Real fragmented-MP4 bytes (see media/): init + per-track fragments.
        # Transport tests use deterministic garbage; finalization tests need
        # media FFmpeg can actually demux, so these routes serve real files.
        m = re.match(r"^/dash/([va])-init\.mp4$", path)
        if m:
            data = media_file(f"{m.group(1)}-init.mp4")
            return self._send_bytes(data if data is not None else b"missing fixture", 200 if data is not None else 500, {"Content-Type": "video/mp4"})
        m = re.match(r"^/dash/([va])-(\d+)\.m4s$", path)
        if m:
            data = media_file(f"{m.group(1)}-{m.group(2)}.m4s")
            return self._send_bytes(data if data is not None else b"missing fixture", 200 if data is not None else 500, {"Content-Type": "video/mp4"})
        if path == "/dash/t-init.mp4":
            data = media_file("v-init.mp4")
            return self._send_bytes(data if data is not None else b"missing fixture", 200 if data is not None else 500, {"Content-Type": "video/mp4"})
        m = re.match(r"^/dash/t-(\d+)\.m4s$", path)
        if m:
            data = media_file(f"v-{int(m.group(1)) - 1}.m4s")
            return self._send_bytes(data if data is not None else b"missing fixture", 200 if data is not None else 500, {"Content-Type": "video/mp4"})
        if path == "/media/sample.mp4":
            size, seed, _ = FILES["sample.mp4"]
            return self._serve_file("sample.mp4", seed, size, True, {"Content-Type": "video/mp4"})
        if path == "/page/video.html":
            body = (
                "<!doctype html><html><body style='margin:40px;background:#222'>"
                "<video width='640' height='360' controls autoplay muted loop "
                "src='/media/real.mp4'></video></body></html>"
            )
            return self._send_bytes(body.encode(), 200, {"Content-Type": "text/html"})
        if path == "/media/real.mp4":
            # Real playable fixture (ffmpeg testsrc). No Range support needed;
            # the browser streams it progressively for playback tests.
            try:
                with open("real.mp4", "rb") as handle:
                    data = handle.read()
            except OSError:
                return self._send_bytes(b"fixture not generated", 500)
            return self._send_bytes(data, 200, {"Content-Type": "video/mp4", "Accept-Ranges": "bytes"})
        return self._send_bytes(b"not found", 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fixtures on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
