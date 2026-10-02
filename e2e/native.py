"""End-to-end checks against the real resident executable (Windows).

Runs a freshly built `download-manager.exe` from an isolated portable folder,
against a local fixture server, and drives it two ways:

* engine scenarios: committed jobs are seeded into the portable SQLite store
  and resumed by the real startup path;
* bridge scenarios: captures are posted to the loopback bridge exactly as the
  extension posts them.

Every scenario names the failure it guards against. The run writes a JSON
artifact to e2e/results/ and exits non-zero if any scenario fails.

Usage:  python e2e/native.py [--no-build] [--keep]
The bridge port (38217) must be free: quit any running Download Manager first.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import devtools
import sealed

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "e2e" / "results"
BRIDGE = "http://127.0.0.1:38217"

spec = importlib.util.spec_from_file_location("fixture", ROOT / "fixtures" / "server.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

MIB = 1024 * 1024
RANGE_BYTES = fixture.file_data("range.bin", fixture.FILES["range.bin"][1], fixture.FILES["range.bin"][0])
NO_RANGE_BYTES = fixture.file_data("no-range.bin", fixture.FILES["no-range.bin"][1], fixture.FILES["no-range.bin"][0])
FALLBACK_BYTES = bytes(range(256)) * (2 * MIB // 256)
LOGIN_PAGE = b"<!DOCTYPE html><html>login required</html>"
EXPORT_BYTES = b"id,account\n1,requested-export\n"
NAMED_BYTES = b"named by the server\n" * 64
counts: collections.Counter = collections.Counter()
# Bandwidth scenario: a large range-capable file whose bytes are timed as the
# server sends them, per job.
METERED = bytes(range(256)) * (32 * MIB // 256)
METER: list[tuple[float, str, int]] = []
# Cookie scenarios: every request under /ck/ is logged with its Cookie header.
SESSION = "ck-" + os.urandom(8).hex()
COOKIE_LOG: list[tuple[float, str, str, str]] = []

# Server pacing (F22): the first request for each range or segment is told
# to come back in a second; a retry inside that second is counted as early.
PACING_FIRST: dict[str, float] = {}
PACING_EARLY: collections.Counter = collections.Counter()
PACED_HLS = "\n".join(["#EXTM3U", "#EXT-X-TARGETDURATION:2"] + [line for i in range(3) for line in ("#EXTINF:2.0,", f"/paced-hls/seg{i}.ts")] + ["#EXT-X-ENDLIST", ""])


def pacing_says_wait(key: str, scenario: str) -> bool:
    now = time.time()
    first = PACING_FIRST.setdefault(key, now)
    if first == now:
        return True
    if now - first < 1.0:
        PACING_EARLY[scenario] += 1
        return True
    return False


# Presentations the assembler cannot reproduce faithfully (F09). Each uses the
# real fixture video, so an engine that ignores the boundary completes a file.
_VIDEO_SET = '<AdaptationSet contentType="video"><Representation id="v"><BaseURL>/dash/</BaseURL><SegmentList><Initialization sourceURL="v-init.mp4"/><SegmentURL media="v-0.m4s"/></SegmentList></Representation></AdaptationSet>'
DASH_BOUNDARY = {
    "/dynamic-spaced.mpd": f'<MPD type = "dynamic" mediaPresentationDuration="PT4S"><Period>{_VIDEO_SET}</Period></MPD>',
    "/two-periods.mpd": f'<MPD type="static" mediaPresentationDuration="PT8S"><Period id="main">{_VIDEO_SET}</Period><Period id="ad">{_VIDEO_SET}</Period></MPD>',
    "/drm.mpd": '<MPD type="static" mediaPresentationDuration="PT4S"><Period>' + _VIDEO_SET.replace('<Representation', '<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/><Representation') + '</Period></MPD>',
}


# Player-evidence presentations: HLS whose playlist URLs look nothing like
# their segments' (as on X: /pl/ playlists, /vid/ segments), so only what a
# playlist lists can tie it to what a player played. Segment ids map to the
# fixture's video (v) and audio (a) tracks.
PE_SEGMENTS = {"7f3a": "v", "91c2": "a", "2d0e": "v"}


def pe_media_playlist(segment_id: str) -> bytes:
    track = PE_SEGMENTS[segment_id]
    count = fixture.DASH_V_SEGS if track == "v" else fixture.DASH_A_SEGS
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:4", "#EXT-X-PLAYLIST-TYPE:VOD", f'#EXT-X-MAP:URI="/pe/seg/{segment_id}/init.mp4"']
    for index in range(count):
        lines += ["#EXTINF:2.0,", f"/pe/seg/{segment_id}/{index}.m4s"]
    return ("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n").encode()


PE_PLAYLISTS = {
    "/pe/a/master.m3u8": b'#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=300000,AUDIO="aud"\nvideo.m3u8\n',
    "/pe/a/video.m3u8": pe_media_playlist("7f3a"),
    "/pe/a/audio.m3u8": pe_media_playlist("91c2"),
    "/pe/b/master.m3u8": b"#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-STREAM-INF:BANDWIDTH=300000\nvideo.m3u8\n",
    "/pe/b/video.m3u8": pe_media_playlist("2d0e"),
}


class Handler(fixture.Handler):
    def _count(self, method: str) -> None:
        counts[(self.path.split("?")[0], method, self.headers.get("Range", ""))] += 1

    def _raw(self, code: int, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _fallback(self, stream_body: bytes, stream_type: str) -> None:
        # First GET: a range-capable binary. The 1 MiB probe succeeds, every
        # worker range is rate-limited, so the engine must fall back to one
        # stream; that stream returns `stream_body`.
        size = len(FALLBACK_BYTES)
        rng = self.headers.get("Range")
        if rng == f"bytes=0-{MIB - 1}":
            return self._raw(206, FALLBACK_BYTES[:MIB], {"Content-Range": f"bytes 0-{MIB - 1}/{size}", "Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"})
        if rng:
            return self._raw(429, b"")
        plain_gets = counts[(self.path, "GET", "")]
        if plain_gets == 1:
            return self._raw(200, FALLBACK_BYTES, {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"})
        body = stream_body + b" " * (size - len(stream_body)) if len(stream_body) < size else stream_body
        return self._raw(200, body, {"Content-Type": stream_type})

    def _metered(self, tag: str) -> None:
        start, end = 0, len(METERED) - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            start, end = int(first), min(int(last) if last else end, end)
        self.send_response(206 if rng else 200)
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(METERED)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            for at in range(start, end + 1, 64 * 1024):
                piece = METERED[at:min(at + 64 * 1024, end + 1)]
                self.wfile.write(piece)
                METER.append((time.time(), tag, len(piece)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _cookie_route(self, path: str):
        """Routes that need the browser's session cookie, like a logged-in site."""
        header = self.headers.get("Cookie", "")
        COOKIE_LOG.append((time.time(), path, self.headers.get("Host", ""), header))
        jar = dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part)
        if path == "/ck/hop.bin":
            # A redirect to another host (localhost is not 127.0.0.1).
            return self._raw(302, b"", {"Location": f"http://localhost:{self.server.server_port}/ck/landing.bin"})
        if path == "/ck/landing.bin":
            return self._raw(200, NAMED_BYTES, {"Content-Type": "application/octet-stream"})
        if jar.get("session") != SESSION:
            return self._raw(403, b"login required")
        if path == "/ck/gated.bin":
            size, seed, _ = fixture.FILES["range.bin"]
            return self._serve_file("range.bin", seed, size, True)
        if path == "/ck/confirm.bin":
            # A confirmation step: the server sets a cookie and redirects back.
            if jar.get("confirm") != "yes":
                return self._raw(302, b"", {"Location": "/ck/confirm.bin?step=2", "Set-Cookie": "confirm=yes; Path=/ck"})
            return self._raw(200, EXPORT_BYTES, {"Content-Type": "text/csv", "Content-Disposition": "attachment; filename=confirm.csv"})
        if path == "/ck/slow.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FALLBACK_BYTES)))
            self.end_headers()
            try:
                for at in range(0, len(FALLBACK_BYTES), 64 * 1024):
                    self.wfile.write(FALLBACK_BYTES[at:at + 64 * 1024])
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return None
        if path.startswith("/ck/hls/"):
            self.path = "/hls/" + self.path[len("/ck/hls/"):]
            return fixture.Handler.do_GET(self)
        return self._raw(404, b"missing")

    def do_GET(self):  # noqa: N802
        self._count("GET")
        path = self.path.split("?")[0]
        if path.startswith("/ck/"):
            return self._cookie_route(path)
        if path.startswith("/metered/"):
            return self._metered(path[len("/metered/"):])
        if path in PE_PLAYLISTS:
            return self._raw(200, PE_PLAYLISTS[path], {"Content-Type": "application/vnd.apple.mpegurl"})
        pe = re.match(r"^/pe/seg/([0-9a-f]{4})/(init\.mp4|\d+\.m4s)$", path)
        if pe and pe.group(1) in PE_SEGMENTS:
            track = PE_SEGMENTS[pe.group(1)]
            data = fixture.media_file(f"{track}-init.mp4" if pe.group(2) == "init.mp4" else f"{track}-{pe.group(2)}")
            if data is not None:
                return self._raw(200, data, {"Content-Type": "video/mp4"})
        # The fixture's fragmented MP4 tracks served whole, as progressive files.
        if path == "/progressive/video.mp4":
            return self._raw(200, b"".join(fixture.media_file(f) for f in ("v-init.mp4", "v-0.m4s", "v-1.m4s", "v-2.m4s")), {"Content-Type": "video/mp4"})
        if path == "/progressive/audio.mp4":
            return self._raw(200, fixture.media_file("a-init.mp4") + fixture.media_file("a-0.m4s"), {"Content-Type": "audio/mp4"})
        if path == "/progressive/audio-labelled-video.mp4":
            # Audio-only MP4 served as video/mp4, as v.redd.it serves its audio.
            return self._raw(200, fixture.media_file("a-init.mp4") + fixture.media_file("a-0.m4s"), {"Content-Type": "video/mp4"})
        if path == "/fallback-html.bin":
            return self._fallback(LOGIN_PAGE, "text/html")
        if path == "/fallback-good.bin":
            return self._fallback(FALLBACK_BYTES, "application/octet-stream")
        if path == "/cookie.bin":
            ok = self.headers.get("Cookie") == "session=ok"
            return self._raw(200 if ok else 403, b"cookie export" if ok else b"forbidden")
        if path == "/login-page.bin":
            return self._raw(200, LOGIN_PAGE, {"Content-Type": "text/html"})
        if path in ("/post-reject.bin", "/post-ok.bin"):
            return self._raw(200, b"GET landing page instead of the POST export", {"Content-Type": "application/octet-stream"})
        if path == "/named/plain.bin":
            return self._raw(200, NAMED_BYTES, {"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="server; plain.bin"'})
        if path == "/named/star.bin":
            return self._raw(200, NAMED_BYTES, {"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=\"fallback.bin\"; filename*=UTF-8''server%20%C3%A9t%C3%A9.bin"})
        if path.startswith("/url-named/"):
            return self._raw(200, NAMED_BYTES, {"Content-Type": "application/octet-stream"})
        if path == "/hls-hole.m3u8":
            data = "\n".join(["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXTINF:2.0,", "/hls/seg0.ts", "#EXTINF:2.0,", "http://[unresolvable", "#EXTINF:2.0,", "/hls/seg1.ts", "#EXT-X-ENDLIST", ""])
            return self._raw(200, data.encode(), {"Content-Type": "application/vnd.apple.mpegurl"})
        if path == "/hls-two-maps.m3u8":
            data = "\n".join(["#EXTM3U", "#EXT-X-TARGETDURATION:2", '#EXT-X-MAP:URI="/dash/v-init.mp4"', "#EXTINF:2.0,", "/dash/v-0.m4s", "#EXT-X-DISCONTINUITY", '#EXT-X-MAP:URI="/dash/a-init.mp4"', "#EXTINF:2.0,", "/dash/a-0.m4s", "#EXT-X-ENDLIST", ""])
            return self._raw(200, data.encode(), {"Content-Type": "application/vnd.apple.mpegurl"})
        if path == "/gone.mpd":
            return self._raw(410, b"expired")
        if path == "/paced/range.bin":
            rng = self.headers.get("Range", "")
            size, seed, _ = fixture.FILES["range.bin"]
            if rng.startswith("bytes=") and not rng.startswith("bytes=0-") and pacing_says_wait(rng, "range"):
                return self._raw(503, b"slow down", {"Retry-After": "1"})
            return self._serve_file("range.bin", seed, size, True)
        if path == "/paced-hls.m3u8":
            return self._raw(200, PACED_HLS.encode(), {"Content-Type": "application/vnd.apple.mpegurl"})
        if path.startswith("/paced-hls/seg"):
            if pacing_says_wait(path, "hls"):
                return self._raw(503, b"slow down", {"Retry-After": "1"})
            index = int(path[len("/paced-hls/seg"):-3])
            return self._raw(200, fixture.file_data(f"seg{index}", 0x80 + index, 188 * 16), {"Content-Type": "video/mp2t"})
        if path in DASH_BOUNDARY:
            return self._raw(200, DASH_BOUNDARY[path].encode(), {"Content-Type": "application/dash+xml"})
        if path == "/zero.mpd":
            data = b'<MPD type="static" mediaPresentationDuration="PT4S"><Period><AdaptationSet contentType="video"><Representation id="v"><SegmentTemplate timescale="1" duration="0" media="s-$Number$.m4s"/></Representation></AdaptationSet></Period></MPD>'
            return self._raw(200, data, {"Content-Type": "application/dash+xml"})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        self._count("POST")
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        path = self.path.split("?")[0]
        if path == "/post-ok.bin":
            return self._raw(200, EXPORT_BYTES, {"Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="export.csv"'})
        return self._raw(403, b"")


# What the Add window's closeSurface() does: destroy(), else close(). It runs
# after the evaluation returns, since the window may take the connection away.
CLOSE_SURFACE = """(() => {
  const invoke = window.__TAURI_INTERNALS__.invoke, label = window.__TAURI_INTERNALS__.metadata.currentWindow.label;
  setTimeout(() => invoke('plugin:window|destroy', { label }).catch(() => invoke('plugin:window|close', { label })).catch(() => {}), 50);
  return label;
})()"""


def zone_identifier(path: Path) -> str | None:
    try:
        return Path(f"{path}:Zone.Identifier").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class Run:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def check(self, scenario: str, guards: str, ok: bool, evidence: dict) -> None:
        self.results.append({"scenario": scenario, "guards": guards, "pass": bool(ok), "evidence": evidence})
        print(f"{'PASS' if ok else 'FAIL'}  {scenario}  - {guards}")


EXTENSION_ORIGIN = "chrome-extension://joniainjojgbpnjjclallmfbdgnebgbe"
PAIRING: dict = {}


def bridge(message: dict, timeout: float = 30) -> dict:
    """One message to the resident over the paired, sealed channel."""
    return sealed.message(PAIRING, message, timeout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the isolated runtime folder")
    parser.add_argument("--exe", help="resident build to run (default: target/debug)")
    args = parser.parse_args()

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 38217)) == 0:
            print("Port 38217 is in use: quit the running Download Manager first.")
            return 2
    if not args.no_build and not args.exe:
        subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "src-tauri" / "Cargo.toml")], check=True)
        subprocess.run(["node", str(ROOT / "node_modules" / "vite" / "bin" / "vite.js"), "build", "--config", str(ROOT / "extension" / "vite.config.ts")], check=True, cwd=ROOT)

    runtime = Path(tempfile.mkdtemp(prefix="dm-e2e-"))
    (runtime / "data").mkdir()
    out = runtime / "out"
    exe = runtime / "download-manager.exe"
    shutil.copy2(Path(args.exe) if args.exe else ROOT / "src-tauri" / "target" / "debug" / "download-manager.exe", exe)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    db = runtime / "data" / "download-manager.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE notifications (id TEXT PRIMARY KEY, payload TEXT);")
    con.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps({"startAtSignIn": False, "showManagerAtSignIn": False, "retryAutomatically": True, "maxRetries": 2, "completionNotifications": False, "failureNotifications": False, "defaultFolder": str(out)}),))
    engine = {
        "range": ("/file/range.bin", {}),
        "single": ("/file/no-range.bin", {}),
        "redirect": ("/redirect", {}),
        "fallback-html": ("/fallback-html.bin", {}),
        "fallback-good": ("/fallback-good.bin", {}),
        "post-reject": ("/post-reject.bin", {"postBody": "export=requested"}),
        "post-ok": ("/post-ok.bin", {"postBody": "export=requested"}),
        "zero-mpd": ("/zero.mpd", {"media": True, "playerKind": "video"}),
        "paced-range": ("/paced/range.bin", {}),
        # Stopped while finalizing: every fragment is on disk, the source has expired.
        "recover-media": ("/gone.mpd", {"media": True, "playerKind": "video", "state": "finalizing", "progress": 100, "segments": {"completed": 6, "total": 6, "identity": "seeded"}}),
        "paced-hls": ("/paced-hls.m3u8", {"media": True, "playerKind": "video"}),
        "mpd-dynamic": ("/dynamic-spaced.mpd", {"media": True, "playerKind": "video"}),
        "mpd-periods": ("/two-periods.mpd", {"media": True, "playerKind": "video"}),
        "mpd-drm": ("/drm.mpd", {"media": True, "playerKind": "video"}),
        "hls-vod": ("/hls/vod.m3u8", {"media": True, "playerKind": "video"}),
        "dual": ("/progressive/video.mp4", {"media": True, "playerKind": "video", "companionAudio": "/progressive/audio.mp4"}),
        "dual-labelled": ("/progressive/video.mp4", {"media": True, "playerKind": "video", "companionAudio": "/progressive/audio-labelled-video.mp4"}),
        "dual-two-videos": ("/progressive/video.mp4", {"media": True, "playerKind": "video", "companionAudio": "/progressive/video.mp4?as-companion"}),
        "hls-hole": ("/hls-hole.m3u8", {"media": True, "playerKind": "video"}),
        "hls-two-maps": ("/hls-two-maps.m3u8", {"media": True, "playerKind": "video"}),
    }
    parts = runtime / "data" / "tmp" / "recover-media.part.segments"
    for track, files in (("00", ["v-init.mp4", "v-0.m4s", "v-1.m4s", "v-2.m4s"]), ("01", ["a-init.mp4", "a-0.m4s"])):
        (parts / track).mkdir(parents=True)
        for index, file in enumerate(files):
            (parts / track / f"{index:08}.part").write_bytes(fixture.media_file(file))
    for name, (path, extra) in engine.items():
        job = dict(id=name, name=f"{name}.bin", source=base + path, domain="127.0.0.1", state="connecting", progress=0, downloaded=0, total=None, speed=0, eta=None, connections=0, maxConnections=4, mode="single-stream", media=False, destination=str(out / f"{name}.bin"), tempPath=str(runtime / "data" / "tmp" / f"{name}.part"), resumable=False, mime=None, error=None, created="2026-09-29T19:00:00Z", started=None, completed=None, provisional=False, segments=None, referrer=base + "/page", events=[])
        job.update(extra)
        if "companionAudio" in extra:
            job["companionAudio"] = base + extra["companionAudio"]
        con.execute("INSERT INTO jobs VALUES (?, ?, ?)", (name, job["created"], json.dumps(job)))
    # Started later, under a global limit (bandwidth scenario).
    for name, cap in (("cap-own", MIB), ("cap-none", None)):
        job = dict(id=name, name=f"{name}.bin", source=f"{base}/metered/{name}", domain="127.0.0.1", state="paused", progress=0, downloaded=0, total=None, speed=0, eta=None, connections=0, maxConnections=4, mode="single-stream", media=False, destination=str(out / f"{name}.bin"), tempPath=str(runtime / "data" / "tmp" / f"{name}.part"), resumable=False, mime=None, error=None, created="2026-09-29T19:00:00Z", started=None, completed=None, provisional=False, segments=None, referrer=base + "/page", events=[], bandwidthLimit=cap)
        con.execute("INSERT INTO jobs VALUES (?, ?, ?)", (name, job["created"], json.dumps(job)))
    con.commit()
    con.close()

    log_path = runtime / "app.log"
    log = open(log_path, "w", encoding="utf-8")
    app = subprocess.Popen([str(exe), "--startup"], cwd=runtime, stdout=log, stderr=log, env=devtools.environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    run = Run()

    def jobs() -> dict[str, dict]:
        with sqlite3.connect(db) as connection:
            return {j["id"]: j for (payload,) in connection.execute("SELECT payload FROM jobs") for j in [json.loads(payload)]}

    try:
        # ---- engine scenarios ------------------------------------------------
        deadline = time.time() + 60
        state: dict[str, dict] = {}
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                state = jobs()
            except sqlite3.Error:
                continue
            if all(state.get(n, {}).get("state") in ("completed", "failed") for n in engine):
                break
        time.sleep(1)
        state = jobs()

        def job(name: str) -> dict:
            j = state.get(name, {})
            dest = Path(j.get("destination", out / f"{name}.bin"))
            data = dest.read_bytes() if dest.is_file() else None
            return {"state": j.get("state"), "mode": j.get("mode"), "error": j.get("error"), "bytes": None if data is None else len(data), "prefix": None if data is None else data[:48].decode("latin-1"), "zone": zone_identifier(dest) if data is not None else None, "_data": data}

        def top_boxes(data: bytes | None) -> list[str]:
            kinds, position = [], 0
            while data and position + 8 <= len(data):
                length = int.from_bytes(data[position:position + 4], "big")
                if length == 1:
                    length = int.from_bytes(data[position + 8:position + 16], "big")
                kinds.append(data[position + 4:position + 8].decode("latin-1"))
                position += max(length, 8)
            return kinds

        def evidence(name: str, **extra) -> dict:
            return {k: v for k, v in {**job(name), **extra}.items() if k != "_data"}

        for name, expected in (("range", RANGE_BYTES), ("redirect", RANGE_BYTES), ("single", NO_RANGE_BYTES)):
            j = job(name)
            run.check(f"engine/{name}", "an ordinary download completes byte-exact", j["state"] == "completed" and j["_data"] == expected, evidence(name))
        j = job("range")
        run.check("engine/mark-of-the-web", "a completed file carries its internet origin (Zone.Identifier ZoneId=3)", bool(j["zone"]) and "ZoneId=3" in j["zone"], evidence("range"))
        j = job("fallback-html")
        run.check("engine/fallback-html", "the one-stream fallback must not complete with a login page", j["state"] == "failed" and j["_data"] is None, evidence("fallback-html"))
        j = job("fallback-good")
        run.check("engine/fallback-good", "a valid one-stream fallback still completes byte-exact", j["state"] == "completed" and j["_data"] == FALLBACK_BYTES, evidence("fallback-good"))
        posts = counts[("/post-reject.bin", "POST", "")]
        gets = sum(v for (p, m, _), v in counts.items() if p == "/post-reject.bin" and m == "GET")
        j = job("post-reject")
        run.check("engine/post-reject", "a rejected POST fails with its status, is not downgraded to GET, and is not replayed", j["state"] == "failed" and j["_data"] is None and "403" in (j["error"] or "") and gets == 0 and posts == 1, evidence("post-reject", posts=posts, gets=gets))
        gets_ok = sum(v for (p, m, _), v in counts.items() if p == "/post-ok.bin" and m == "GET")
        j = job("post-ok")
        run.check("engine/post-ok", "an accepted POST completes with the POST response and never issues a GET", j["state"] == "completed" and j["_data"] == EXPORT_BYTES and gets_ok == 0, evidence("post-ok", gets=gets_ok))
        j = job("zero-mpd")
        run.check("engine/zero-duration-mpd", "a malformed manifest fails visibly instead of leaving the job stuck connecting", j["state"] == "failed" and bool(j["error"]), evidence("zero-mpd"))

        for name, expect, guards in (
            ("mpd-dynamic", "Live media", "a live MPD is refused even when its type attribute is written with spaces"),
            ("mpd-periods", "more than one Period", "a multi-Period MPD is refused instead of stretching each Period into its own track"),
            ("mpd-drm", "DRM-protected", "a DRM-protected MPD is refused instead of saving undecryptable bytes"),
        ):
            j = job(name)
            run.check(f"engine/{name}", guards, j["state"] == "failed" and expect in (j["error"] or "") and j["_data"] is None, evidence(name))

        j = job("recover-media")
        asked = sum(v for (p, _, _), v in counts.items() if p == "/gone.mpd")
        run.check("engine/recover-finalizing-from-disk", "a media job that stopped while finalizing is finished from its downloaded fragments, without asking the expired source again", j["state"] == "completed" and (j["_data"] or b"")[4:8] == b"ftyp" and asked == 0, evidence("recover-media", sourceRequests=asked))

        j = job("paced-range")
        run.check("engine/paced-range", "range workers told Retry-After wait it out: the download completes byte-exact with no retry inside the server's window", j["state"] == "completed" and j["_data"] == RANGE_BYTES and PACING_EARLY["range"] == 0, evidence("paced-range", earlyRetries=PACING_EARLY["range"], pacedRanges=sum(1 for k in PACING_FIRST if k.startswith("bytes="))))
        j = job("paced-hls")
        run.check("engine/paced-hls", "media segments told Retry-After wait it out: the playlist completes with no retry inside the server's window", j["state"] == "completed" and bool(j["bytes"]) and PACING_EARLY["hls"] == 0, evidence("paced-hls", earlyRetries=PACING_EARLY["hls"]))

        j = job("hls-vod")
        run.check("engine/hls-vod", "an ordinary finite HLS playlist still assembles (control)", j["state"] == "completed" and bool(j["bytes"]), evidence("hls-vod"))
        j = job("hls-hole")
        run.check("engine/hls-unresolvable-fragment", "a fragment line that cannot be addressed fails the job instead of leaving a silent gap", j["state"] == "failed" and "cannot be resolved" in (j["error"] or ""), evidence("hls-hole"))
        j = job("hls-two-maps")
        run.check("engine/hls-map-switch", "a playlist that switches initialization maps is refused, not assembled under the first map", j["state"] == "failed" and "initialization map" in (j["error"] or ""), evidence("hls-two-maps"))

        j = job("dual")
        stored = state.get("dual", {})
        shown = devtools.evaluate("""(async () => {
          document.querySelector('[aria-label="Select dual.bin"]').click();
          await new Promise((r) => setTimeout(r, 300));
          const label = [...document.querySelectorAll('.inspector *')].find((e) => e.children.length === 0 && e.textContent === 'Transfer mode');
          return label?.parentElement?.textContent ?? null;
        })()""")
        run.check("engine/dual-track", "separate video and audio streams complete into one regular MP4 (indexed, no fragments), are not claimed resumable, and the inspector names the mode", j["state"] == "completed" and top_boxes(j["_data"]) == ["ftyp", "moov", "mdat"] and stored.get("mode") == "dual-track" and stored.get("resumable") is False and "Separate video and audio" in (shown or ""), evidence("dual", resumable=stored.get("resumable"), inspector=shown, boxes=top_boxes(j["_data"])))

        j = job("dual-labelled")
        run.check("engine/dual-track-audio-labelled-video", "an audio track served as video/mp4 is still the audio: the two tracks complete into one file", j["state"] == "completed" and (j["_data"] or b"")[4:8] == b"ftyp", evidence("dual-labelled"))
        j = job("dual-two-videos")
        run.check("engine/dual-track-two-videos", "a companion that holds video, not audio, fails the job with that reason instead of muxing two pictures", j["state"] == "failed" and "holds video, not audio" in (j["error"] or ""), evidence("dual-two-videos"))

        # ---- bandwidth: a job's own cap under a global limit -----------------
        # Both jobs run together. Rates are measured from the bytes the server
        # sent while both ran, skipping the first two seconds (a full bucket's
        # burst and the socket buffers filling).
        global_mib, cap_mib = 4, 1
        devtools.invoke("update_settings", {"patch": {"bandwidthLimit": global_mib, "bandwidthUnit": "MB/s"}})
        reattach = devtools.evaluate("""(async () => {
          const invoke = window.__TAURI_INTERNALS__.invoke;
          const state = async (id) => (await invoke('get_snapshot')).jobs.find((job) => job.id === id).state;
          await invoke('resume_job', { id: 'cap-own' });
          await invoke('resume_job', { id: 'cap-none' });
          const started = performance.now();
          // What the app reports for the capped job once it has run a while.
          const reported = [];
          while (performance.now() - started < 40000) {
            const jobs = (await invoke('get_snapshot')).jobs;
            const other = jobs.find((job) => job.id === 'cap-none');
            const own = jobs.find((job) => job.id === 'cap-own');
            if (own.state === 'downloading' && performance.now() - started > 3000) reported.push({ speed: own.speed, etaSeconds: own.etaSeconds ?? null, note: own.note ?? null, total: own.total ?? null });
            if (other.state !== 'downloading' && other.state !== 'connecting') break;
            await new Promise((r) => setTimeout(r, 100));
          }
          // Reattach asked of a running and of a completed download.
          await invoke('reattach_job', { id: 'cap-own' });
          await invoke('reattach_job', { id: 'range' });
          await new Promise((r) => setTimeout(r, 300));
          const after = { running: await state('cap-own'), completed: await state('range') };
          await invoke('pause_job', { id: 'cap-own' });
          return { after, reported };
        })()""", timeout=60)
        devtools.invoke("update_settings", {"patch": {"bandwidthLimit": None}})
        reported = (reattach or {}).get("reported") or []
        reattach = (reattach or {}).get("after")
        # Once bytes flow: the status note is gone, and over the later half of
        # the run the speed is about the cap and the time left a number.
        flowing = [item for item in reported if item["speed"] > 0]
        steady = flowing[len(flowing) // 2:]
        speeds = sorted(item["speed"] for item in steady)
        median = speeds[len(speeds) // 2] / 1024 ** 2 if speeds else 0
        run.check("engine/speed-reported", "a download capped at 1 MiB/s reports about that speed, a number of seconds left, and no leftover status note",
                  len(steady) >= 5 and 0.75 <= median <= 1.25
                  and all(item["note"] is None for item in flowing)
                  and all(isinstance(item["etaSeconds"], int) and item["etaSeconds"] > 0 for item in steady if item["total"]),
                  {"samples": len(reported), "flowing": len(flowing), "median_mib_s": round(median, 3), "speeds_kib": [item["speed"] // 1024 for item in reported], "last": reported[-2:]})
        sent = list(METER)
        rates: dict = {}
        if sent:
            begin = sent[0][0] + 2.0
            end = max((at for at, tag, _ in sent if tag == "cap-none"), default=begin)
            if end - begin >= 2.0:
                for tag in ("cap-own", "cap-none"):
                    rates[tag] = round(sum(n for at, t, n in sent if t == tag and begin <= at <= end) / (end - begin) / MIB, 2)
                rates["seconds"] = round(end - begin, 2)
        own_ok = 0 < rates.get("cap-own", 99) <= cap_mib * 1.2
        total_ok = global_mib * 0.75 <= rates.get("cap-own", 0) + rates.get("cap-none", 0) <= global_mib * 1.2
        run.check("engine/reattach-stopped-only", "Reattach leaves a running and a completed download as they are", reattach == {"running": "downloading", "completed": "completed"}, {"after": reattach})
        run.check("engine/job-cap-under-global-limit", f"with a {global_mib} MiB/s global limit, a job capped at {cap_mib} MiB/s never exceeds its cap while the two together still use the global limit", own_ok and total_ok, {"MiBps": rates})

        # ---- bridge scenarios ------------------------------------------------
        def capture(path: str, capture_id: str, viable: bool = True) -> dict:
            # The store encrypts sources at rest, so each capture is found by a
            # unique file name instead.
            payload = {"source": base + path, "name": f"{capture_id}.bin", "pageUrl": base + "/page", "referrer": base + "/page", "captureId": capture_id}
            if viable:
                payload["requireViable"] = True
            return bridge({"type": "capture-acquisition", "payload": payload})

        def job_ids_for(capture_id: str) -> list[str]:
            return [j["id"] for j in jobs().values() if j.get("name") == f"{capture_id}.bin" and j.get("provisional")]

        # Only the browser extension with the pinned ID (and local non-browser
        # clients, which send no Origin) may use the bridge.
        probe = {"request": "origin-probe-0000000000"}
        other = sealed.post("/v1/pair/status", probe, {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"})[0]
        page = sealed.post("/v1/pair/status", probe, {"Origin": "https://example.com"})[0]
        ours = sealed.post("/v1/pair/status", probe, {"Origin": EXTENSION_ORIGIN})[0]
        run.check("bridge/extension-origin-pinned", "another extension and a web page are refused; the extension with the pinned ID is accepted", other == 403 and page == 403 and ours == 200, {"otherExtension": other, "webPage": page, "ours": ours})

        # ---- pairing and the sealed channel --------------------------------
        declined = sealed.pair(allow=False)
        run.check("pairing/declined", "a pairing the user declines yields no key", declined.get("state") == "denied" and "key" not in declined, {k: v for k, v in declined.items() if k != "key"})
        # Allow on a request a newer one replaced (or that expired) records
        # nothing, and says so instead of closing as if it had paired.
        stale, fresh = (base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=").replace("_", "-") for _ in range(2))
        sealed.post("/v1/pair", {"request": stale})
        sealed.post("/v1/pair", {"request": fresh})
        stale_answer = devtools.evaluate(f"window.__TAURI_INTERNALS__.invoke('answer_pairing', {{ id: {json.dumps(stale)}, allow: true }}).then(() => ({{ ok: true }}), (error) => ({{ error: String(error) }}))")
        stale_status = sealed.post("/v1/pair/status", {"request": stale})[1].get("state")
        devtools.invoke("answer_pairing", {"id": fresh, "allow": False})
        run.check("pairing/stale-allow", "allowing a request that was replaced reports that it expired and pairs nothing", "expired" in (stale_answer.get("error") or "") and stale_status == "unknown", {"answer": stale_answer, "staleStatus": stale_status})
        PAIRING.update(sealed.pair(allow=True))
        run.check("pairing/approved", "a pairing the user allows yields a key, once", PAIRING.get("state") == "approved" and len(PAIRING.get("key", "")) == 44 and sealed.post("/v1/pair/status", {"request": PAIRING.get("request")})[1].get("state") == "unknown", {k: v for k, v in PAIRING.items() if k != "key"})
        probe_message = {"type": "cancel-acquisition", "payload": {"captureId": "sealed-probe"}}
        plain_status, plain = sealed.post("/v1/message", probe_message)
        legacy_status, _ = sealed.post("/v1/capture", probe_message)
        run.check("sealed/plain-refused", "an unsealed message is refused, and the old unsealed routes are gone", plain_status == 400 and legacy_status == 404, {"plain": [plain_status, plain], "legacyRoute": legacy_status})
        stranger = {"keyId": "0000000000000000", "key": PAIRING["key"]}
        status, answer = sealed.post("/v1/message", sealed.seal(stranger, probe_message)[0])
        run.check("sealed/unknown-key", "a message under a key the app never paired is told to pair", status == 401 and answer.get("paired") is False, {"status": status, "answer": answer})
        body, nonce = sealed.seal(PAIRING, probe_message)
        first, envelope = sealed.post("/v1/message", body)
        again, _ = sealed.post("/v1/message", body)
        stale, _ = sealed.post("/v1/message", sealed.seal(PAIRING, probe_message, sent_at=time.time() - 300)[0])
        run.check("sealed/replay-and-stale", "a replayed or five-minute-old message is refused", first == 200 and again == 400 and stale == 400, {"first": first, "replayed": again, "stale": stale})
        opened = sealed.open_answer(PAIRING, nonce, envelope)
        try:
            sealed.open_answer(PAIRING, sealed.seal(PAIRING, probe_message)[1], envelope)
            rebound = True
        except Exception:
            rebound = False
        run.check("sealed/answer-bound", "the answer opens only for the request it answers", opened.get("ok") is True and not rebound, {"opened": opened, "opensForAnotherRequest": rebound})

        reply = capture("/file/range.bin", "cap-viable")
        run.check("bridge/viable", "a fetchable capture is accepted with a job id", reply.get("ok") is True and isinstance(reply.get("id"), str), {"reply": reply})
        run.check("bridge/lookup-is-live", "the harness can see a live provisional job by name (guards the negative checks below)", job_ids_for("cap-viable") == [reply.get("id")], {"found": job_ids_for("cap-viable")})
        dup = capture("/file/range.bin", "cap-viable")
        run.check("bridge/duplicate-capture-id", "a repeated capture id returns the same job, never a second owner", dup.get("id") == reply.get("id"), {"first": reply, "second": dup})
        cancel = bridge({"type": "cancel-acquisition", "payload": {"captureId": "cap-viable"}})
        time.sleep(1)
        run.check("bridge/cancel-by-capture-id", "cancelling by capture id removes the provisional job", cancel.get("ok") is True and reply.get("id") not in jobs(), {"cancel": cancel})

        for path, label in (("/cookie.bin", "session-gated 403"), ("/login-page.bin", "HTML login page")):
            started = time.time()
            capture_id = "cap-" + label.replace(" ", "-")
            reply = capture(path, capture_id)
            time.sleep(1)
            leftovers = job_ids_for(capture_id)
            run.check(f"bridge/handback {label}", f"a {label} is handed back to the browser and leaves no job behind", reply.get("ok") is False and reply.get("handback") is True and not leftovers, {"reply": reply, "seconds": round(time.time() - started, 2), "leftoverJobs": leftovers})

        # Chromium's naming precedence: server filename > download attribute > URL.
        def named(path: str, capture_id: str, name: str | None, hint: bool) -> str | None:
            payload = {"source": base + path, "pageUrl": base + "/page", "captureId": capture_id, "requireViable": True, **({"name": name} if name else {}), **({"nameIsHint": True} if hint else {})}
            reply = bridge({"type": "capture-acquisition", "payload": payload})
            found = None
            for _ in range(20):
                time.sleep(0.25)
                found = jobs().get(reply.get("id") or "", {}).get("name")
                if found and found != name:
                    break
            bridge({"type": "cancel-acquisition", "payload": {"captureId": capture_id}})
            return found
        got = named("/named/plain.bin", "cap-name-hint", "hint-from-link.bin", True)
        run.check("bridge/name-server-beats-hint", "a link's name is a hint: the server's Content-Disposition filename wins", got == "server; plain.bin", {"name": got})
        got = named("/named/star.bin", "cap-name-star", None, False)
        run.check("bridge/name-rfc8187", "with no name at all, an RFC 8187 filename* is decoded and preferred over filename", got == "server été.bin", {"name": got})
        got = named("/url-named/My%20Report%20%C3%A9t%C3%A9.bin", "cap-name-url", None, False)
        run.check("bridge/name-url-decoded", "a name taken from the URL is percent-decoded, as a browser saves it", got == "My Report été.bin", {"name": got})
        got = named("/url-named/" + "a" * 300 + ".bin", "cap-name-long", None, False)
        run.check("bridge/name-length-capped", "an overlong name is shortened to a Windows-safe length and keeps its extension", bool(got) and len(got) <= 180 and got.endswith(".bin"), {"name": got, "length": len(got or "")})
        got = named("/named/plain.bin", "cap-name-explicit", "chosen-by-browser.bin", False)
        run.check("bridge/name-explicit-kept", "a name the browser already decided is not replaced", got == "chosen-by-browser.bin", {"name": got})

        # ---- browser cookies (SPEC §16) ------------------------------------
        now = time.time()

        def cookie(name: str, value: str, **extra) -> dict:
            return {"name": name, "value": value, "domain": "127.0.0.1", "hostOnly": True, "path": "/", "secure": False, "httpOnly": True, "sameSite": "lax", **extra}

        session = cookie("session", SESSION)
        # Loopback counts as a secure context (Chromium's rule, and the jar's),
        # so a Secure cookie is sent here; on a plain-http public host it is not.
        never = [
            cookie("otherpath", "p-" + SESSION, path="/elsewhere"),
            cookie("expired", "e-" + SESSION, expirationDate=now - 60),
        ]
        cross_page = f"http://localhost:{server.server_port}/page"

        def take(kind: str, path: str, name: str, cookies: list, page: str | None = None, extra: dict | None = None) -> dict:
            payload = {"source": base + path, "name": name, "pageUrl": page or base + "/page", "captureId": "cap-" + name, "requireViable": True, "cookies": cookies, **(extra or {})}
            return bridge({"type": kind, "payload": payload}, timeout=60)

        def commit_and_wait(job_id: str, name: str) -> dict:
            devtools.invoke("commit_provisional", {"id": job_id, "input": {"name": name, "destination": str(out / name)}})
            for _ in range(120):
                time.sleep(0.25)
                found = jobs().get(job_id, {})
                if found.get("state") in ("completed", "failed"):
                    return found
            return jobs().get(job_id, {})

        def cookies_sent(prefix: str, since: float = 0) -> list[str]:
            return [header for at, path, _, header in COOKIE_LOG if path.startswith(prefix) and at >= since]

        def leaked(headers: list[str]) -> list[str]:
            return [name for name in ("otherpath", "expired", "strict", "laxonly") if any(f"{name}=" in header for header in headers)]

        reply = take("capture-acquisition", "/ck/gated.bin", "ck-gated.bin", [session, cookie("secureonly", "s-" + SESSION, secure=True), *never, cookie("strict", "t-" + SESSION, sameSite="strict")], page=cross_page)
        done = commit_and_wait(reply.get("id", ""), "ck-gated.bin") if reply.get("ok") else {}
        sent = cookies_sent("/ck/gated.bin")
        data = (out / "ck-gated.bin").read_bytes() if (out / "ck-gated.bin").is_file() else b""
        run.check("cookies/logged-in-download", "a download that needs the browser's session completes byte-exact, every request (each range worker) carrying the session cookie", reply.get("ok") is True and done.get("state") == "completed" and data == RANGE_BYTES and sent and all(f"session={SESSION}" in header for header in sent), {"reply": reply, "state": done.get("state"), "requests": len(sent), "withSession": sum(f"session={SESSION}" in header for header in sent)})
        run.check("cookies/scoped-like-chromium", "cookies Chromium would not send here stay behind (another path, expired, Strict from another site's page), while a Secure one goes to loopback as Chromium sends it", not leaked(sent) and all("secureonly=" in header for header in sent), {"leaked": leaked(sent), "secureOnLoopback": all("secureonly=" in header for header in sent)})

        reply = take("capture-acquisition", "/ck/hop.bin", "ck-hop.bin", [session])
        done = commit_and_wait(reply.get("id", ""), "ck-hop.bin") if reply.get("ok") else {}
        landing = [(host, header) for _, path, host, header in COOKIE_LOG if path == "/ck/landing.bin"]
        run.check("cookies/redirect-to-another-host", "a redirect to another host carries none of the original host's cookies", done.get("state") == "completed" and landing and all(SESSION not in header for _, header in landing), {"reply": reply, "state": done.get("state"), "landing": landing})

        reply = take("capture-acquisition", "/ck/confirm.bin", "ck-confirm.csv", [session])
        done = commit_and_wait(reply.get("id", ""), "ck-confirm.csv") if reply.get("ok") else {}
        confirmed = [header for header in cookies_sent("/ck/confirm.bin") if "confirm=yes" in header]
        run.check("cookies/set-by-server", "a cookie the server sets along the way (a confirmation step) is kept for that download", done.get("state") == "completed" and bool(confirmed), {"reply": reply, "state": done.get("state"), "requestsWithConfirm": len(confirmed)})

        reply = take("media-capture", "/ck/hls/vod.m3u8", "ck-media.ts", [cookie("session", SESSION, sameSite="no_restriction"), cookie("laxonly", "l-" + SESSION)], page=cross_page, extra={"playerKind": "video"})
        done = commit_and_wait(reply.get("id", ""), "ck-media.ts") if reply.get("ok") else {}
        sent = cookies_sent("/ck/hls/")
        run.check("cookies/media-from-another-site", "a gated HLS stream played on another site completes with its SameSite=None session on every request, and without its Lax cookie", done.get("state") == "completed" and len(sent) > 2 and all(f"session={SESSION}" in header for header in sent) and not leaked(sent), {"reply": reply, "state": done.get("state"), "requests": len(sent), "leaked": leaked(sent)})

        exposed = {
            "uiSnapshot": SESSION in json.dumps(devtools.invoke("get_snapshot")),
            "database": any(SESSION.encode() in path.read_bytes() for path in (runtime / "data").glob("download-manager.db*")),
            "appLog": SESSION in log_path.read_text(encoding="utf-8", errors="replace"),
        }
        run.check("cookies/never-exposed", "cookie values reach neither the UI, nor the database in readable form, nor the log", not any(exposed.values()), exposed)
        with sqlite3.connect(db) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
        run.check("cookies/erased-when-done", "once those downloads complete, their cookies are gone from storage", rows == 0, {"credentialRows": rows})

        # ---- Save durability: the real Add window, driven by UI Automation ----
        uia_timeouts: list[tuple[str, ...]] = []

        def uia(*args: str) -> tuple[int, str]:
            # UI Automation blocks on WebView2 windows while the Windows session
            # is locked. A hung call is a failed attempt, and after two the Save
            # scenarios fail fast instead of waiting it out.
            if len(uia_timeouts) >= 2:
                return 1, ""
            try:
                done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "e2e" / "uia.ps1"), *args], capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                uia_timeouts.append(args)
                return 1, ""
            return done.returncode, done.stdout

        reply = capture("/file/range.bin", "cap-save")
        ready = False
        for _ in range(60):
            code, listing = uia("-List", "-Seconds", "2")
            if "Ready to save" in listing:
                ready = True
                break
            time.sleep(0.5)
        lock = sqlite3.connect(db, timeout=0, isolation_level=None)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            clicked = uia("-Button", "Save")[1].strip()
            # Each refused write waits out SQLite's busy timeout (5 s) first.
            code, during = 1, ""
            for _ in range(30):
                time.sleep(1)
                code, during = uia("-List", "-Seconds", "5")
                if code != 0 or "Could not record the Save" in during:
                    break
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        stayed_open = code == 0 and "Save" in during
        told_why = "Could not record the Save" in during
        stored = next((j for j in jobs().values() if j.get("name") == "cap-save.bin"), {})
        run.check("save/unrecorded-save-is-not-acknowledged", "when the Save cannot be written, the Add window stays open with the storage error and the job stays provisional", ready and stayed_open and told_why and stored.get("provisional") is True, {"reply": reply, "ready": ready, "clicked": clicked, "windowAfter": during.splitlines()[:16], "storedProvisional": stored.get("provisional"), "uiaTimeouts": len(uia_timeouts)})
        uia("-Button", "Save")
        final = {}
        for _ in range(40):
            time.sleep(0.5)
            final = next((j for j in jobs().values() if j.get("name") == "cap-save.bin"), {})
            if final.get("state") == "completed":
                break
        saved_file = Path(final.get("destination", "")).is_file() if final else False
        run.check("save/retry-after-storage-recovers", "once storage accepts writes again, the same Save completes the download", final.get("state") == "completed" and final.get("provisional") is False and saved_file, {"state": final.get("state"), "provisional": final.get("provisional"), "fileExists": saved_file})

        # The Add window closed the way its X closes it when the job has not
        # reached the window yet.
        reply = capture("/file/range.bin", "cap-closed")
        closing = None
        for _ in range(40):
            try:
                closing = devtools.evaluate(CLOSE_SURFACE, timeout=10, page=f"window=add&id={reply.get('id')}$")
                break
            except Exception as error:
                closing = str(error)
                time.sleep(0.25)
        time.sleep(1.5)
        try:
            still_open = devtools.evaluate("document.title", timeout=10, page=f"window=add&id={reply.get('id')}$") is not None
        except Exception:
            still_open = False
        leftovers = job_ids_for("cap-closed")
        run.check("bridge/add-window-closed", "the Add window's own close works, and closing it unsaved discards the capture", reply.get("ok") is True and not still_open and not leftovers, {"reply": reply, "window": closing, "stillOpen": still_open, "leftoverJobs": leftovers})
        for i in leftovers:
            bridge({"type": "cancel-acquisition", "payload": {"id": i}})

        early = bridge({"type": "cancel-acquisition", "payload": {"captureId": "cap-early"}})
        late = capture("/file/range.bin", "cap-early")
        time.sleep(1)
        leftovers = job_ids_for("cap-early")
        run.check("bridge/cancel-before-create", "a timeout cancel that arrives first stops the late create from leaving an orphan owner", late.get("ok") is False and not leftovers, {"cancel": early, "create": late, "leftoverJobs": leftovers})
        for i in leftovers:
            bridge({"type": "cancel-acquisition", "payload": {"id": i}})

        # ---- extension worker scenarios (real background.ts, real bridge) ------
        worker = subprocess.run(["node", str(ROOT / "e2e" / "extension.mjs"), base], capture_output=True, text=True, encoding="utf-8", timeout=240, env={**os.environ, "DM_PAIRING": json.dumps({"keyId": PAIRING["keyId"], "key": PAIRING["key"]})})
        try:
            report = json.loads(worker.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            report = {"scenarios": [], "handedOver": []}
            run.check("extension/harness", "the worker harness ran", False, {"stdout": worker.stdout[-2000:], "stderr": worker.stderr[-2000:]})
        for item in report["scenarios"]:
            run.check(f"extension/{item['scenario']}", item["guards"], item["pass"], item["evidence"])
        time.sleep(1.5)
        stored = jobs()
        # A capture whose source the app decides: wait until its job has
        # downloaded (or failed), then check what it settled on.
        for item in report["handedOver"]:
            expect = item.get("expect")
            if not expect:
                continue
            job_id, settled = item.get("jobId"), {}
            for _ in range(120):
                # The app's own view: sources are stored encrypted.
                snapshot = devtools.invoke("get_snapshot") or {}
                settled = next((j for j in snapshot.get("jobs", []) if j.get("id") == job_id), {})
                if settled.get("state") in ("ready", "completed", "failed"):
                    break
                time.sleep(0.25)
            fetched = sorted({path for (path, _, _) in counts if any(path.startswith(prefix) for prefix in expect.get("notRequested", []))})
            ok = settled.get("state") in expect["states"] and (settled.get("source") or "").endswith(expect.get("source", "")) and expect.get("error", "") in (settled.get("error") or "") and not fetched
            run.check(f"extension/{item['scenario']} app", expect["guards"], ok, {"state": settled.get("state"), "source": settled.get("source"), "error": settled.get("error"), "events": [event.get("message") for event in settled.get("events", [])][:4], "requestedButShouldNotBe": fetched})
        stored = jobs()
        for item in report["handedOver"]:
            owners = [j["id"] for j in stored.values() if j.get("name") == item.get("name") and j.get("provisional")]
            if item.get("expectJob"):
                run.check(f"extension/{item['scenario']} resident owner", "an accepted handoff leaves exactly one resident owner", len(owners) == 1, {"name": item.get("name"), "owners": owners})
            elif not item.get("jobId"):
                run.check(f"extension/{item['scenario']} no resident owner", "a handed-back or unanswered capture leaves no resident job", not owners, {"name": item.get("name"), "owners": owners})
            bridge({"type": "cancel-acquisition", "payload": {"captureId": item.get("captureId")}})

        # ---- where partial files live ------------------------------------
        # Next to the file they become, unless the user set a temp folder;
        # the file grows as ranges land instead of being sized up front
        # (sizing it makes exFAT write zeros over all of it first).
        def wait_job(job_id: str, until, seconds: float = 30) -> dict:
            deadline = time.time() + seconds
            while time.time() < deadline:
                found = next((j for j in devtools.invoke("get_snapshot")["jobs"] if j["id"] == job_id), {})
                if until(found):
                    return found
                time.sleep(0.2)
            return next((j for j in devtools.invoke("get_snapshot")["jobs"] if j["id"] == job_id), {})

        folder_a, folder_b = out / "temp-a", out / "temp-b"
        folder_a.mkdir(exist_ok=True)
        app_tmp_before = sorted(p.name for p in (runtime / "data" / "tmp").glob("*")) if (runtime / "data" / "tmp").is_dir() else []
        moving_id = devtools.invoke("create_provisional", {"input": {"source": f"{base}/metered/temp-move", "name": "temp-move.bin", "destination": str(folder_a / "temp-move.bin"), "bandwidthLimit": 512 * 1024}})
        running = wait_job(moving_id, lambda j: j.get("state") == "downloading" and j.get("downloaded", 0) > 2 * MIB)
        part = Path(running.get("tempPath", ""))
        part_size = part.stat().st_size if part.is_file() else None
        app_tmp_after = sorted(p.name for p in (runtime / "data" / "tmp").glob("*")) if (runtime / "data" / "tmp").is_dir() else []
        run.check("temp/next-to-target", "with no temp folder set, the partial file sits next to the file it becomes, not in the app's folder", part.parent == folder_a and part.is_file() and part.name.startswith("temp-move.bin.") and app_tmp_after == app_tmp_before, {"tempPath": str(part), "appTmpNew": sorted(set(app_tmp_after) - set(app_tmp_before))})
        run.check("engine/no-preallocation", "the partial file grows as ranges land: it is never sized to the whole download up front", part_size is not None and part_size < len(METERED), {"partSize": part_size, "total": len(METERED), "downloaded": running.get("downloaded")})
        devtools.invoke("commit_provisional", {"id": moving_id, "input": {"name": "temp-move.bin", "destination": str(folder_b / "temp-move.bin"), "bandwidthLimit": None}})
        moved = wait_job(moving_id, lambda j: Path(j.get("tempPath", "")).parent == folder_b or j.get("state") in ("completed", "failed"))
        done = wait_job(moving_id, lambda j: j.get("state") in ("completed", "failed"), 60)
        final = folder_b / "temp-move.bin"
        left = sorted(p.name for folder in (folder_a, folder_b) for p in folder.glob("*.part*"))
        run.check("temp/follows-save", "Save to another folder moves the partial file there and the download resumes from it: byte-exact, nothing left behind", Path(moved.get("tempPath", "")).parent == folder_b and done.get("state") == "completed" and final.is_file() and final.read_bytes() == METERED and not left, {"tempPathAfterSave": moved.get("tempPath"), "state": done.get("state"), "error": done.get("error"), "leftovers": left, "events": [e.get("message") for e in done.get("events", [])][:6]})

        explicit = runtime / "explicit-temp"
        devtools.invoke("update_settings", {"patch": {"tempFolder": str(explicit)}})
        explicit_id = devtools.invoke("create_provisional", {"input": {"source": f"{base}/metered/temp-explicit", "name": "temp-explicit.bin", "destination": str(folder_a / "temp-explicit.bin"), "bandwidthLimit": 512 * 1024}})
        running = wait_job(explicit_id, lambda j: j.get("state") == "downloading" and j.get("downloaded", 0) > MIB)
        devtools.invoke("commit_provisional", {"id": explicit_id, "input": {"name": "temp-explicit.bin", "destination": str(folder_b / "temp-explicit.bin"), "bandwidthLimit": None}})
        done = wait_job(explicit_id, lambda j: j.get("state") in ("completed", "failed"), 60)
        final = folder_b / "temp-explicit.bin"
        devtools.invoke("update_settings", {"patch": {"tempFolder": ""}})
        cleared = devtools.invoke("get_snapshot")["settings"].get("tempFolder")
        run.check("temp/explicit-folder", "a temp folder the user set holds the partial file even when Save picks another folder; clearing the setting goes back to next-to-the-file", Path(running.get("tempPath", "")).parent == explicit and done.get("state") == "completed" and final.is_file() and final.read_bytes() == METERED and cleared is None and not list(explicit.glob("*")), {"tempPath": running.get("tempPath"), "state": done.get("state"), "error": done.get("error"), "clearedSetting": cleared, "explicitLeft": [p.name for p in explicit.glob("*")]})

        # ---- updates to the windows carry only what changed -------------------
        captured = devtools.evaluate("""(async () => {
          const internals = window.__TAURI_INTERNALS__;
          const seen = [];
          const handler = internals.transformCallback((event) => seen.push(event.payload));
          await internals.invoke('plugin:event|listen', { event: 'state-delta', target: { kind: 'Any' }, handler });
          const before = (await internals.invoke('get_snapshot')).settings.density;
          await internals.invoke('update_settings', { patch: { density: before === 'compact' ? 'comfortable' : 'compact' } });
          await new Promise((resolve) => setTimeout(resolve, 500));
          await internals.invoke('update_settings', { patch: { density: before } });
          await new Promise((resolve) => setTimeout(resolve, 500));
          const snapshot = await internals.invoke('get_snapshot');
          return { jobsInList: snapshot.jobs.length, revision: snapshot.revision, updates: seen.map((delta) => ({ revision: delta.revision, jobs: delta.jobs.length, settings: delta.settings ? delta.settings.density : null })) };
        })()""")
        updates = captured.get("updates", [])
        with_settings = [update for update in updates if update["settings"]]
        revisions = [update["revision"] for update in updates]
        run.check("ui/updates-carry-changes", "a settings change reaches the windows as an update with the new settings and none of the unchanged jobs, numbered in order", captured.get("jobsInList", 0) > 0 and len(with_settings) >= 2 and all(update["jobs"] == 0 for update in with_settings) and revisions == sorted(revisions) and captured.get("revision", 0) >= max(revisions or [0]), captured)

        # ---- cookies across a restart -------------------------------------
        reply = take("capture-acquisition", "/ck/slow.bin", "ck-slow.bin", [session])
        slow_id = reply.get("id", "")
        devtools.invoke("commit_provisional", {"id": slow_id, "input": {"name": "ck-slow.bin", "destination": str(out / "ck-slow.bin")}})
        devtools.invoke("pause_job", {"id": slow_id})
        time.sleep(1)
        with sqlite3.connect(db) as connection:
            row = connection.execute("SELECT payload FROM credentials WHERE id = ?", (slow_id,)).fetchone()
        stored = row[0] if row else ""
        paused_state = jobs().get(slow_id, {}).get("state")
        app.terminate()
        app.wait(timeout=15)
        log.close()
        log = open(log_path, "a", encoding="utf-8")
        app = subprocess.Popen([str(exe), "--startup"], cwd=runtime, stdout=log, stderr=log, env=devtools.environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        restarted = time.time()
        for _ in range(60):
            try:
                devtools.invoke("resume_job", {"id": slow_id}, timeout=10)
                break
            except Exception:
                time.sleep(0.5)
        done = {}
        for _ in range(120):
            time.sleep(0.25)
            done = jobs().get(slow_id, {})
            if done.get("state") in ("completed", "failed"):
                break
        after = cookies_sent("/ck/slow.bin", since=restarted)
        with sqlite3.connect(db) as connection:
            remaining = connection.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
        run.check("cookies/survive-restart", "a committed download's cookies are kept protected (DPAPI) on disk, carry it through an app restart, and are erased when it completes", paused_state == "paused" and stored.startswith("dpapi1:") and SESSION not in stored and done.get("state") == "completed" and bool(after) and all(f"session={SESSION}" in header for header in after) and remaining == 0, {"pausedState": paused_state, "storedProtected": stored.startswith("dpapi1:"), "storedReadable": SESSION in stored, "state": done.get("state"), "requestsAfterRestart": len(after), "credentialRowsAfter": remaining})

        # Forgetting pairings in Settings: the old key stops working.
        devtools.invoke("forget_pairings")
        status, answer = sealed.post("/v1/message", sealed.seal(PAIRING, probe_message)[0])
        run.check("pairing/forget", "after Forget in Settings the browser's key is refused and it must pair again", status == 401 and answer.get("paired") is False, {"status": status, "answer": answer})
    finally:
        app.terminate()
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
        log.close()
        server.shutdown()

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
    artifact = {"run": stamp, "commit": commit, "dirtyTree": dirty, "passed": sum(r["pass"] for r in run.results), "failed": sum(not r["pass"] for r in run.results), "scenarios": run.results, "serverRequests": {f"{p} {m} {r}".strip(): v for (p, m, r), v in sorted(counts.items())}, "appLog": log_path.read_text(encoding="utf-8", errors="replace")[-4000:]}
    target = RESULTS / f"native-{stamp}.json"
    target.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"\n{artifact['passed']} passed, {artifact['failed']} failed -> {target.relative_to(ROOT)}")
    if not args.keep:
        shutil.rmtree(runtime, ignore_errors=True)
    else:
        print(f"runtime kept at {runtime}")
    return 0 if artifact["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
