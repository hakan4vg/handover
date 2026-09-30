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
counts: collections.Counter = collections.Counter()


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

    def do_GET(self):  # noqa: N802
        self._count("GET")
        path = self.path.split("?")[0]
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
    args = parser.parse_args()

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 38217)) == 0:
            print("Port 38217 is in use: quit the running Download Manager first.")
            return 2
    if not args.no_build:
        subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "src-tauri" / "Cargo.toml")], check=True)

    runtime = Path(tempfile.mkdtemp(prefix="dm-e2e-"))
    (runtime / "data").mkdir()
    out = runtime / "out"
    exe = runtime / "download-manager.exe"
    shutil.copy2(ROOT / "src-tauri" / "target" / "debug" / "download-manager.exe", exe)

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
    }
    for name, (path, extra) in engine.items():
        job = dict(id=name, name=f"{name}.bin", source=base + path, domain="127.0.0.1", state="connecting", progress=0, downloaded=0, total=None, speed=0, eta=None, connections=0, maxConnections=4, mode="single-stream", media=False, destination=str(out / f"{name}.bin"), tempPath=str(runtime / "data" / "tmp" / f"{name}.part"), resumable=False, mime=None, error=None, created="2026-09-29T19:00:00Z", started=None, completed=None, provisional=False, segments=None, referrer=base + "/page", events=[])
        job.update(extra)
        con.execute("INSERT INTO jobs VALUES (?, ?, ?)", (name, job["created"], json.dumps(job)))
    con.commit()
    con.close()

    log_path = runtime / "app.log"
    log = open(log_path, "w", encoding="utf-8")
    app = subprocess.Popen([str(exe), "--startup"], cwd=runtime, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
