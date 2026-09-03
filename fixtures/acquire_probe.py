#!/usr/bin/env python3
"""acquire_once path proofs (no GUI input, no XTEST).

Exercises the acquisition paths the limiter probe does not, against the real
binary under Xvfb :99 with an isolated HOME, polling the jobs SQLite table:

- redirect: /redirect (302 -> /file/range.bin) completes byte-identical.
- one-use: a minted single-fetch URL completes when the app is the sole
  consumer (documents the double-consume hazard: a parallel browser fetch
  would 410 the app's copy).
- retry-503: /status/503 fails honestly after bounded automatic retries;
  the job events must show the retry attempts, not a single shot.

Each mode runs the app once and asserts on DB state only.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

BIN = "/srv/repos/downloadmanager/src-tauri/target/debug/download-manager"
HOME = os.environ.get("DM_HOME", "/tmp/dm-acquire-test")
DB = os.path.join(HOME, ".local/share/com.downloadmanager.app/download-manager.db")
MODE = os.environ.get("DM_MODE", "redirect")


def env():
    return dict(
        os.environ,
        HOME=HOME,
        # Scope the temp dir too: the app resolves it via the cache dir,
        # which follows XDG_CACHE_HOME, not HOME.
        XDG_CACHE_HOME=os.path.join(HOME, ".cache"),
        DISPLAY=":99",
        WEBKIT_DISABLE_COMPOSITING_MODE="1",
    )


def wait_db(timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(DB):
            return
        time.sleep(0.5)
    raise RuntimeError("database never appeared")


def get_jobs():
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(DB)
    try:
        rows = con.execute("SELECT payload FROM jobs").fetchall()
    finally:
        con.close()
    return [json.loads(payload) for (payload,) in rows]


def wait_state(want, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = get_jobs()
        if jobs and all(job.get("state") in want for job in jobs):
            return jobs
        time.sleep(0.5)
    raise RuntimeError(f"states never reached {want}: {[j.get('state') for j in get_jobs()]}")


def run_capture(source, name, extra_payload=None):
    payload = {"source": source, "name": name}
    if extra_payload:
        payload.update(extra_payload)
    capture = json.dumps({"type": "capture-acquisition", "payload": payload})
    return subprocess.Popen(
        [BIN, "--capture", capture],
        env=env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def temp_bytes(job, size, timeout=60):
    temp_path = job["tempPath"]
    for _ in range(int(timeout * 2)):
        if os.path.exists(temp_path) and os.path.getsize(temp_path) == size:
            break
        time.sleep(0.5)
    with open(temp_path, "rb") as handle:
        return handle.read()


def redirect_mode():
    app = run_capture("http://127.0.0.1:8901/redirect", "redirect.bin")
    try:
        (job,) = wait_state(("finalizing", "completed"))
        actual = temp_bytes(job, 8 * 1024 * 1024)
        expected = urllib.request.urlopen(
            "http://127.0.0.1:8901/file/range.bin", timeout=60
        ).read()
        assert actual == expected, "redirect bytes differ from range.bin"
        print(f"REDIRECT: PASS ({len(actual)} bytes identical via 302)", flush=True)
    finally:
        app.terminate()
        app.wait(timeout=15)


def one_use_mode():
    with urllib.request.urlopen(
        "http://127.0.0.1:8901/one-use/mint", timeout=10
    ) as response:
        app_url = response.read().decode().strip()
    with urllib.request.urlopen(
        "http://127.0.0.1:8901/one-use/mint", timeout=10
    ) as response:
        direct_url = response.read().decode().strip()
    expected = urllib.request.urlopen(direct_url, timeout=30).read()
    assert len(expected) == 64 * 1024, len(expected)
    app = run_capture(app_url, "one-use.bin")
    try:
        (job,) = wait_state(("finalizing", "completed"))
        actual = temp_bytes(job, 64 * 1024)
        assert actual == expected, "one-use bytes differ between mints"
        print(
            f"ONE-USE: PASS ({len(actual)} bytes, sole consumer succeeds)",
            flush=True,
        )
    finally:
        app.terminate()
        app.wait(timeout=15)


def retry_mode():
    app = run_capture("http://127.0.0.1:8901/status/503", "unavailable.bin")
    try:
        (job,) = wait_state(("failed",), timeout=120)
        events = " | ".join(event.get("message", "") for event in job.get("events", []))
        print(f"error={job.get('error')} events={events[:220]}", flush=True)
        assert job.get("error") and "503" in job["error"], job.get("error")
        assert "retry" in events.lower(), "no bounded retry attempts recorded"
        print("RETRY-503: PASS (failed honestly after bounded retries)", flush=True)
    finally:
        app.terminate()
        app.wait(timeout=15)


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)
    print(f"mode={MODE}", flush=True)
    {"redirect": redirect_mode, "one-use": one_use_mode, "retry-503": retry_mode}[
        MODE
    ]()
    print("ACQUIRE-PROBE: PASS", flush=True)


if __name__ == "__main__":
    sys.exit(main())
