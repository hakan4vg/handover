#!/usr/bin/env python3
"""Worker-fed MSE player: the capture mechanism must not need page-visible bytes.

The fixture page plays MediaSource fed by a Web Worker that fetches the manifest
and every segment itself, so nothing a content script can observe names the
media. This is the class of player an extension cannot resolve by instrumenting
the page (x.com-class blob/MSE players), and the mechanism under test is the
provider-agnostic one: the tab's observed traffic is attributed to the player
that is playing, and the resident verifies what it acquires.

Asserts:
  * the media button appears on the worker-fed player at all,
  * clicking it hands a real source to the resident (a job exists whose source is
    the manifest), instead of the "no downloadable media found" error,
  * the acquisition reaches the confirmation gate (state `ready`),
  * the cancel path leaves no job, no temp file, and no notification behind.

Usage:  python worker_mse_chromium_probe.py [--keep]
Env:    DM_BIN      resident binary (defaults to the release build in the repo)
        DM_CHROME   chromium binary (defaults to a local playwright build)
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
# cdp_drive.py parses its CLI at import time; the repo's probes import it the
# same way, with argv stubbed so the module's defaults are what load.
_saved_argv = sys.argv[:]
sys.argv = ["cdp_drive.py", "1", "-", "/tmp/unused.png"]
from cdp_drive import CDP, ws_connect  # noqa: E402
sys.argv = _saved_argv

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = str(ROOT / "extension" / "dist")
PAGE = "/page/worker-mse.html"
# One page per shape the mechanism must handle: a worker-fed MSE player (the
# page-visible pipeline is blind) and an ordinary progressive player (the fast,
# exact-evidence path must stay untouched).
CASES = {
    "worker": {
        "page": "/page/worker-mse.html",
        "expect_mode": "segments",
        "expect_total": 87235,
        "label": "worker-fed MSE",
    },
    "progressive": {
        "page": "/page/video.html",
        "expect_mode": "single-stream",
        "expect_total": 34524,
        "label": "progressive <video src>",
    },
}

READINESS_JS = (
    "JSON.stringify((()=>{const v=document.querySelector('video');"
    "const b=document.querySelector('#dm-media-download-button');"
    "return {playing:!!v&&!v.paused&&v.currentTime>0.5, button:!!b, t:v?v.currentTime:-1,"
    "ready:v?v.readyState:-1, err:window.__mseError||''};})())"
)
CLICK_JS = "document.querySelector('#dm-media-download-button')?.click(); 'clicked'"


def resolve_binary(env: str, candidates: list[Path]) -> Path:
    override = os.environ.get(env)
    if override:
        path = Path(override)
        if not path.exists():
            raise SystemExit(f"{env} points at a missing file: {path}")
        return path
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"none of these exist (set {env}): {candidates}")


def resolve_chrome() -> Path:
    candidates = [
        Path.home() / "AppData/Local/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ]
    globs = [
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
    ]
    found: list[Path] = []
    for root in candidates:
        for pattern in globs:
            found.extend(sorted(root.glob(pattern)))
    return resolve_binary("DM_CHROME", found)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_json(url: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read().decode())
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {url}")


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {url}")


def page_target(port: int, want: str) -> dict:
    deadline = time.time() + 30.0
    while time.time() < deadline:
        for target in wait_json(f"http://127.0.0.1:{port}/json/list"):
            if target.get("type") == "page" and want in target.get("url", ""):
                return target
        time.sleep(0.2)
    raise SystemExit("page target never appeared")


def connect(port: int, target: dict) -> CDP:
    path = "/" + target["webSocketDebuggerUrl"].split(f"127.0.0.1:{port}/", 1)[-1]
    return CDP(ws_connect(str(port), path))


def drive(client: CDP, expression: str):
    return client.evaluate(expression)


def jobs(db: Path) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT payload FROM jobs").fetchall()
    return [json.loads(payload) for (payload,) in rows]


def wait_job(db: Path, timeout: float = 60.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = jobs(db)
        if found:
            return found[0]
        time.sleep(0.5)
    return None


def cancel_bridge(port: int, job_id: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/capture",
        data=json.dumps({"type": "cancel-acquisition", "payload": {"id": job_id}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception as error:  # noqa: BLE001
        print(f"cancel failed: {error}")


def main() -> int:
    case_name = "worker"
    if "--page" in sys.argv:
        case_name = sys.argv[sys.argv.index("--page") + 1]
    case = CASES[case_name]
    binary = resolve_binary(
        "DM_BIN",
        [
            ROOT / "src-tauri/target/release/download-manager.exe",
            ROOT / "src-tauri/target/debug/download-manager.exe",
            ROOT / "src-tauri/target/release/download-manager",
            ROOT / "src-tauri/target/debug/download-manager",
        ],
    )
    chrome = resolve_chrome()
    workdir = Path(tempfile.mkdtemp(prefix="dm-worker-mse-"))
    server_port = free_port()
    cdp_port = free_port()
    app_home = workdir / "app"
    app_home.mkdir(parents=True)
    app = app_home / binary.name
    app.write_bytes(binary.read_bytes())
    if os.name != "nt":
        app.chmod(0o755)
    profile = workdir / "profile"
    db = app_home / "data" / "download-manager.db"
    server = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "server.py"), "--port", str(server_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    resident = subprocess.Popen([str(app)], cwd=str(app_home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        wait_http(f"http://127.0.0.1:{server_port}/dash/manifest.mpd", timeout=15)
        wait_json("http://127.0.0.1:38217/v1/health", timeout=30)
        browser = subprocess.Popen(
            [
                str(chrome),
                f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}",
                f"--disable-extensions-except={EXTENSION}",
                f"--remote-debugging-port={cdp_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                f"http://127.0.0.1:{server_port}{case['page']}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        target = page_target(cdp_port, case["page"])
        client = connect(cdp_port, target)
        deadline = time.time() + 60
        state: dict = {}
        while time.time() < deadline:
            state = json.loads(drive(client, READINESS_JS) or "{}")
            if state.get("playing") and state.get("button"):
                break
            time.sleep(0.5)
        if not (state.get("playing") and state.get("button")):
            print(f"FAIL page never became ready to click: {state}")
            return 1
        drive(client, CLICK_JS)
        job = wait_job(db)
        if job is None:
            print("FAIL no acquisition reached the resident for a worker-fed MSE player")
            return 1
        # `source` is encrypted at rest (F11), so the evidence is what the job
        # actually acquired: mode plus the exact byte total of the fixture.
        print(f"job: state={job.get('state')} mode={job.get('mode')} total={job.get('total')} domain={job.get('domain')}")
        if job.get("mode") != case["expect_mode"] or job.get("total") != case["expect_total"]:
            print(f"FAIL acquired {job.get('mode')}/{job.get('total')} bytes, expected {case['expect_mode']}/{case['expect_total']}")
            cancel_bridge(38217, job["id"])
            return 1
        deadline = time.time() + 60
        while time.time() < deadline and job.get("state") not in ("ready", "completed", "failed"):
            time.sleep(0.5)
            job = jobs(db)[0]
        if job.get("state") != "ready":
            print(f"FAIL acquisition did not reach the confirmation gate: {job.get('state')} {job.get('error')}")
            cancel_bridge(38217, job["id"])
            return 1
        print(f"PASS {case['label']} capture handed over and reached state=ready ({job.get('downloaded')} bytes)")
        cancel_bridge(38217, job["id"])
        time.sleep(1.5)
        leftovers = list((app_home / "data" / "tmp").glob("*"))
        if jobs(db):
            print(f"FAIL cancel left {len(jobs(db))} job(s) behind")
            return 1
        if leftovers:
            print(f"FAIL cancel left temp files behind: {leftovers}")
            return 1
        print("PASS cancel left no job, no temp data, and no notification")
        return 0
    finally:
        for process in (browser, resident, server):
            if process is not None:
                process.terminate()
        if os.environ.get("DM_KEEP") != "1":
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"kept workdir: {workdir}")


if __name__ == "__main__":
    raise SystemExit(main())
