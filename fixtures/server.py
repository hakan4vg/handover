#!/usr/bin/env python3
"""Deterministic download-engine fixture server (SPEC §18).

Stdlib only. Serves the shapes the transfer engine must handle:
  range-capable file / range-ignoring file / slow-drip file
  redirects (single + chain) / token auth / one-use URL / changed identity
  finite HLS (TS) / HLS master+variant / live HLS (must be rejected)
  static DASH SegmentList with separate audio+video / SegmentTemplate /
  dynamic DASH (must be rejected) / progressive media
  retryable 503 / plain 404

All payload bytes are deterministic (seeded PRNG stream), so resume and
identity checks are reproducible.

Usage:  python3 server.py [--port 8901]
"""

import argparse
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SEG_TS_COUNT = 6
DASH_V_SEGS = 3
DASH_A_SEGS = 2


def stream(seed: int, offset: int, length: int) -> bytes:
    # Deterministic byte stream: xorshift-ish, reproducible at any offset.
    out = bytearray()
    state = (seed ^ 0x9E3779B9) & 0xFFFFFFFF
    # Advance to offset cheaply (lengths here are small; simple loop is fine).
    for i in range(offset + length):
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        if i >= offset:
            out.append((state ^ (i & 0xFF)) & 0xFF)
    return bytes(out)


FILES = {
    # name: (size, seed, range_support)
    "range.bin": (8 * 1024 * 1024, 0x11, True),
    "no-range.bin": (2 * 1024 * 1024, 0x22, False),
    "slow.bin": (512 * 1024, 0x33, True),
    "token.bin": (1 * 1024 * 1024, 0x44, True),
    "changed.bin": (256 * 1024, 0x55, True),
    "sample.mp4": (1 * 1024 * 1024, 0x66, True),
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
            self._send_bytes(stream(seed, start, end - start + 1), 206, headers)
            return
        if ranged:
            headers["Accept-Ranges"] = "bytes"
        # no-range files deliberately omit Accept-Ranges and ignore Range.
        if not ranged and range_header:
            pass
        self._send_bytes(stream(seed, 0, total) if self.command == "GET" or True else b"", 200, headers)

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
            data = stream(seed, 0, size)
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
                    self.wfile.write(stream(seed, off, min(32 * 1024, size - off)))
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
        if path == "/one-use/mint":
            with ONE_USE_LOCK:
                ONE_USE_COUNTER[0] += 1
                token = f"t{ONE_USE_COUNTER[0]}"
                ONE_USE[token] = stream(0x77, 0, 64 * 1024)
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
        if path == "/hls/vod.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
            for i in range(SEG_TS_COUNT):
                lines += ["#EXTINF:2.0,", f"seg{i}.ts"]
            lines.append("#EXT-X-ENDLIST")
            return self._send_bytes(("\n".join(lines) + "\n").encode(), 200, {"Content-Type": "application/vnd.apple.mpegurl"})
        if path == "/hls/live.m3u8":
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg0.ts\n"
            return self._send_bytes(body.encode(), 200, {"Content-Type": "application/vnd.apple.mpegurl"})
        m = re.match(r"^/hls/(seg(\d+)\.ts)$", path)
        if m:
            idx = int(m.group(2))
            return self._send_bytes(stream(0x80 + idx, 0, 188 * 16), 200, {"Content-Type": "video/mp2t"})
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
        m = re.match(r"^/dash/([vat]-[\w-]+\.m4s|t-init\.mp4|t-\d+\.m4s)$", path)
        if m:
            name = m.group(1)
            seed = sum(name.encode()) & 0xFF
            return self._send_bytes(stream(seed, 0, 32 * 1024), 200, {"Content-Type": "video/mp4"})
        if path == "/media/sample.mp4":
            size, seed, _ = FILES["sample.mp4"]
            return self._serve_file("sample.mp4", seed, size, True, {"Content-Type": "video/mp4"})
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
