"""Real-browser session: the resident plus a test page, for a person (or a
computer-use agent) driving Chrome with the unpacked extension.

  python e2e/browser_session.py [--no-build]

Builds the frontend, the extension and the resident, runs the resident from an
isolated portable folder (so your real data is untouched), and serves
http://127.0.0.1:8902/e2e/ with signed-in and public downloads. Press Enter in
this window when the browser steps are done: the resident's final job store
and each saved file's Mark-of-the-Web record are written to
e2e/results/browser-<stamp>.json.
"""
from __future__ import annotations

import argparse
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
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8902
spec = importlib.util.spec_from_file_location("fixture", ROOT / "fixtures" / "server.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

PAGE = b"""<!doctype html><meta charset="utf-8"><title>Download Manager e2e</title>
<style>body{font:15px system-ui;margin:32px;max-width:720px} li{margin:10px 0} code{background:#eee;padding:1px 4px}</style>
<h1>Download Manager e2e page</h1>
<p>Session: <b id="s">%SESSION%</b> &middot; <a href="/e2e/login">Sign in</a> &middot; <a href="/e2e/logout">Sign out</a></p>
<ol>
<li id="a"><a href="/e2e/private.txt" download>A. Private report (link with download attribute, needs sign-in)</a></li>
<li id="b"><a href="/e2e/private-attachment">B. Private export (plain link, server sends it as an attachment, needs sign-in)</a></li>
<li id="c"><a href="/e2e/public.txt" download>C. Public notes (link with download attribute)</a></li>
<li id="d"><a href="/e2e/public-big">D. Public 64 MB archive (plain link, attachment)</a></li>
<li id="e"><button type="button" onclick="document.getElementById('scripted').click()">E. Page script clicks a hidden download link</button><a id="scripted" href="/e2e/scripted.txt" download hidden></a></li>
</ol>
"""


REQUESTS: list[dict] = []


class Handler(fixture.Handler):
    def log_request(self, code="-", size="-"):
        if self.path.startswith("/e2e/") and self.path not in ("/e2e/", "/e2e/favicon.ico"):
            # The resident forwards the browser's User-Agent; only Chrome itself sends Sec-Fetch-*.
            REQUESTS.append({"at": dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], "method": self.command, "path": self.path, "status": int(code) if str(code).isdigit() else code, "client": "chrome" if self.headers.get("Sec-Fetch-Mode") else "resident", "cookie": "session=ok" in (self.headers.get("Cookie") or ""), "range": self.headers.get("Range")})

    def _send(self, code: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        signed_in = "session=ok" in (self.headers.get("Cookie") or "")
        if path in ("/e2e", "/e2e/"):
            body = PAGE.replace(b"%SESSION%", b"signed in" if signed_in else b"signed out")
            return self._send(200, body, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
        if path == "/e2e/login":
            return self._send(302, b"", {"Set-Cookie": "session=ok; Path=/", "Location": "/e2e/"})
        if path == "/e2e/logout":
            return self._send(302, b"", {"Set-Cookie": "session=; Path=/; Max-Age=0", "Location": "/e2e/"})
        if path in ("/e2e/private.txt", "/e2e/private-attachment"):
            if not signed_in:
                return self._send(403, b"forbidden: sign in first", {"Content-Type": "text/plain"})
            name = "private-report.txt" if path.endswith(".txt") else "private-export.txt"
            return self._send(200, b"private export, only for the signed-in session\n" * 2000, {"Content-Type": "text/plain", "Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/e2e/public.txt":
            return self._send(200, b"public notes\n" * 4000, {"Content-Type": "text/plain", "Content-Disposition": 'attachment; filename="public-notes.txt"'})
        if path == "/e2e/scripted.txt":
            return self._send(200, b"scripted download
" * 1000, {"Content-Type": "text/plain", "Content-Disposition": 'attachment; filename="scripted-notes.txt"'})
        if path == "/e2e/public-big":
            size = 64 * 1024 * 1024
            return self._serve_file("public-big.bin", 0x5A, size, True, {"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="public-archive.bin"'})
        return super().do_GET()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    for port in (38217, PORT):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                print(f"Port {port} is in use. Quit the running Download Manager (and any earlier session) first.")
                return 2
    if not args.no_build:
        npm = shutil.which("npm") or "npm"
        subprocess.run([npm, "run", "build:all"], cwd=ROOT, check=True)
        subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "src-tauri" / "Cargo.toml")], check=True)

    runtime = Path(tempfile.mkdtemp(prefix="dm-browser-e2e-"))
    (runtime / "data").mkdir()
    out = runtime / "saved"
    exe = runtime / "download-manager.exe"
    shutil.copy2(ROOT / "src-tauri" / "target" / "debug" / "download-manager.exe", exe)
    db = runtime / "data" / "download-manager.db"
    with sqlite3.connect(db) as con:
        con.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE notifications (id TEXT PRIMARY KEY, payload TEXT);")
        con.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps({"startAtSignIn": False, "showManagerAtSignIn": True, "completionNotifications": True, "failureNotifications": True, "defaultFolder": str(out)}),))

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log = open(runtime / "app.log", "w", encoding="utf-8")
    app = subprocess.Popen([str(exe)], cwd=runtime, stdout=log, stderr=log)
    print("\nResident running from", runtime)
    print("Extension to load unpacked:", ROOT / "extension" / "dist")
    print("Resident saves into:", out)
    print(f"Test page: http://127.0.0.1:{PORT}/e2e/")
    (runtime / "requests.json").write_text("[]", encoding="utf-8")

    def mirror_requests() -> None:
        # Live copy of the request log, for whoever is driving the browser.
        while True:
            threading.Event().wait(1)
            (runtime / "requests.json").write_text(json.dumps(REQUESTS, indent=1), encoding="utf-8")

    threading.Thread(target=mirror_requests, daemon=True).start()
    try:
        input("\nDo the browser steps, then press Enter here to record the results... ")
    except EOFError:
        pass
    try:
        with sqlite3.connect(db) as con:
            stored = [json.loads(payload) for (payload,) in con.execute("SELECT payload FROM jobs")]
    finally:
        app.terminate()
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
        log.close()
        server.shutdown()

    def zone(path: str) -> str | None:
        try:
            return Path(f"{path}:Zone.Identifier").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    jobs = [{"name": j.get("name"), "state": j.get("state"), "provisional": j.get("provisional"), "error": j.get("error"), "destination": j.get("destination"), "fileExists": Path(j.get("destination", "")).is_file(), "zoneIdentifier": zone(j.get("destination", "")), "events": [e.get("message") for e in j.get("events", [])][:8]} for j in stored]
    saved = sorted(p.name for p in out.glob("*")) if out.is_dir() else []
    results = ROOT / "e2e" / "results"
    results.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = results / f"browser-{stamp}.json"
    target.write_text(json.dumps({"run": stamp, "residentJobs": jobs, "residentSavedFiles": saved, "requests": REQUESTS, "appLog": (runtime / "app.log").read_text(encoding="utf-8", errors="replace")[-4000:]}, indent=2), encoding="utf-8")
    print("Results written to", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
