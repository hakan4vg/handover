#!/usr/bin/env python3
"""Headless aggregate-limiter proof (no GUI input, no XTEST).

Launches the real compiled binary under Xvfb :99 with an isolated HOME,
seeds settings bandwidthLimit=1 MB/s directly in its SQLite DB, starts a
provisional capture of the 8 MiB deterministic fixture, and polls the jobs
table to measure the effective transfer rate. A ~1 MB/s plateau on loopback
(which otherwise moves >50 MB/s) proves the shared token bucket paces real
acquisition work end to end.
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
HOME = "/tmp/dm-limit-test"
DB = os.path.join(HOME, ".local/share/com.downloadmanager.app/download-manager.db")
CAPTURE_URL = "http://127.0.0.1:8901/file/range.bin"
LIMIT_BPS = 1 * 1024 * 1024


def env():
    return dict(
        os.environ,
        HOME=HOME,
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
    con = sqlite3.connect(DB)
    try:
        rows = con.execute("SELECT payload FROM jobs").fetchall()
    finally:
        con.close()
    return [json.loads(payload) for (payload,) in rows]


def set_bandwidth_limit():
    con = sqlite3.connect(DB)
    try:
        (payload,) = con.execute(
            "SELECT payload FROM settings WHERE id = 1"
        ).fetchone()
        settings = json.loads(payload)
        settings["bandwidthLimit"] = 1
        settings["bandwidthUnit"] = "MB/s"
        con.execute(
            "UPDATE settings SET payload = ? WHERE id = 1",
            (json.dumps(settings),),
        )
        con.commit()
    finally:
        con.close()


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)

    # Pass 1: boot once so the app creates its DB and settings row.
    boot = subprocess.Popen(
        [BIN], env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_db()
    finally:
        boot.terminate()
        boot.wait(timeout=15)
    set_bandwidth_limit()
    print("seeded bandwidthLimit=1 MB/s", flush=True)

    # Pass 2: real capture of the 8 MiB fixture.
    capture = json.dumps(
        {
            "type": "capture-acquisition",
            "payload": {"source": CAPTURE_URL, "name": "range.bin"},
        }
    )
    app = subprocess.Popen(
        [BIN, "--capture", capture],
        env=env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    samples = []
    job = None
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(0.5)
            jobs = get_jobs()
            if not jobs:
                continue
            job = jobs[0]
            samples.append(
                (time.time(), job.get("downloaded", 0), job.get("state"))
            )
            if job.get("state") in ("finalizing", "completed"):
                break
        states = sorted({state for _, _, state in samples})
        print(f"observed states: {states}", flush=True)
        downloading = [
            (t, n) for (t, n, s) in samples if s == "downloading" and n > 0
        ]
        assert len(downloading) >= 6, f"too few downloading samples: {len(samples)}"
        (t0, n0), (t1, n1) = downloading[0], downloading[-1]
        rate = (n1 - n0) / max(t1 - t0, 0.01)
        print(
            f"downloaded {n0} -> {n1} bytes over {t1 - t0:.1f}s = {rate / 1e6:.2f} MB/s",
            flush=True,
        )
        assert 0.4e6 <= rate <= 1.7e6, f"rate {rate} outside paced band"
        print("RATE-ASSERT: PASS (≈1 MB/s limit honored on loopback)", flush=True)

        if job.get("state") in ("finalizing", "completed"):
            temp_path = job["tempPath"]
            for _ in range(60):
                if os.path.exists(temp_path) and os.path.getsize(temp_path) == 8 * 1024 * 1024:
                    break
                time.sleep(0.5)
            actual = open(temp_path, "rb").read()
            expected = urllib.request.urlopen(CAPTURE_URL, timeout=60).read()
            assert actual == expected, "provisional bytes differ from fixture"
            print(
                f"BYTE-IDENTITY: PASS ({len(actual)} bytes identical to fixture)",
                flush=True,
            )
        else:
            print(
                f"final state {job.get('state')}; byte-identity skipped, rate proof stands",
                flush=True,
            )
        print("LIMITER-PROBE: PASS", flush=True)
    finally:
        app.terminate()
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    sys.exit(main())
