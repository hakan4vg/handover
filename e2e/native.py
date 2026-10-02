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
import collections
import datetime as dt
import importlib.util
import json
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

    def do_GET(self):  # noqa: N802
        self._count("GET")
        path = self.path.split("?")[0]
        if path.startswith("/metered/"):
            return self._metered(path[len("/metered/"):])
        # The fixture's fragmented MP4 tracks served whole, as progressive files.
        if path == "/progressive/video.mp4":
            return self._raw(200, b"".join(fixture.media_file(f) for f in ("v-init.mp4", "v-0.m4s", "v-1.m4s", "v-2.m4s")), {"Content-Type": "video/mp4"})
        if path == "/progressive/audio.mp4":
            return self._raw(200, fixture.media_file("a-init.mp4") + fixture.media_file("a-0.m4s"), {"Content-Type": "audio/mp4"})
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


def bridge(message: dict, timeout: float = 30) -> dict:
    request = urllib.request.Request(f"{BRIDGE}/v1/capture", data=json.dumps(message).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return json.loads(error.read() or b"{}")


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
          while (performance.now() - started < 40000) {
            const other = (await invoke('get_snapshot')).jobs.find((job) => job.id === 'cap-none');
            if (other.state !== 'downloading' && other.state !== 'connecting') break;
            await new Promise((r) => setTimeout(r, 100));
          }
          // Reattach asked of a running and of a completed download.
          await invoke('reattach_job', { id: 'cap-own' });
          await invoke('reattach_job', { id: 'range' });
          await new Promise((r) => setTimeout(r, 300));
          const after = { running: await state('cap-own'), completed: await state('range') };
          await invoke('pause_job', { id: 'cap-own' });
          return after;
        })()""", timeout=60)
        devtools.invoke("update_settings", {"patch": {"bandwidthLimit": None}})
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
        worker = subprocess.run(["node", str(ROOT / "e2e" / "extension.mjs"), base], capture_output=True, text=True, encoding="utf-8", timeout=240)
        try:
            report = json.loads(worker.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            report = {"scenarios": [], "handedOver": []}
            run.check("extension/harness", "the worker harness ran", False, {"stdout": worker.stdout[-2000:], "stderr": worker.stderr[-2000:]})
        for item in report["scenarios"]:
            run.check(f"extension/{item['scenario']}", item["guards"], item["pass"], item["evidence"])
        time.sleep(1.5)
        stored = jobs()
        for item in report["handedOver"]:
            owners = [j["id"] for j in stored.values() if j.get("name") == item.get("name") and j.get("provisional")]
            if item.get("expectJob"):
                run.check(f"extension/{item['scenario']} resident owner", "an accepted handoff leaves exactly one resident owner", len(owners) == 1, {"name": item.get("name"), "owners": owners})
            elif not item.get("jobId"):
                run.check(f"extension/{item['scenario']} no resident owner", "a handed-back or unanswered capture leaves no resident job", not owners, {"name": item.get("name"), "owners": owners})
            bridge({"type": "cancel-acquisition", "payload": {"captureId": item.get("captureId")}})
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
