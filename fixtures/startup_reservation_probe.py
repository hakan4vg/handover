#!/usr/bin/env python3
"""Real-binary proof that a persisted destination marker is reclaimed on startup."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

import reattach_probe as reattach
import segmented_restart_probe as support

TOTAL = 256 * 1024
PARTIAL = 64 * 1024
JOB_ID = "startup-reservation-recovery"
MARKER_PREFIX = "download-manager-reservation-v1:"


def update_seed(db: str, job_id: str, expected: bytes, marker: str) -> tuple[str, str]:
    with sqlite3.connect(db) as connection:
        payload = connection.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        job = json.loads(payload)
        temp_path = job["tempPath"]
        destination = job["destination"]
        Path(temp_path).write_bytes(expected)
        job.update(
            state="downloading",
            downloaded=TOTAL,
            progress=100.0,
            eta=None,
            completedRanges=[{"start": 0, "end": TOTAL - 1}],
            destinationReservation=marker,
        )
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(marker.encode())
        connection.execute("UPDATE jobs SET payload = ? WHERE id = ?", (json.dumps(job), job_id))
        connection.commit()
    return temp_path, destination


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-startup-reservation-")
    xvfb = None
    upstream = None
    proxy = None
    app = None
    try:
        xvfb, display = reattach.start_xvfb()
        support.DISPLAY = display
        upstream_port = support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, support.FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(support.FIXTURE_SERVER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        support.wait_http(f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1")
        proxy = reattach.CountingProxy(upstream_port)
        source = f"http://127.0.0.1:{proxy.port}/changed.bin?variant=1"
        with urlopen(source) as response:
            expected = response.read()
        assert len(expected) == TOTAL
        expected_hash = hashlib.sha256(expected).hexdigest()
        proxy.reset()

        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
        temp_path = reattach.seed_job(
            db,
            home,
            source,
            JOB_ID,
            "managed.bin",
            expected,
            '"v1"',
            state="downloading",
        )
        marker = f"{MARKER_PREFIX}{os.urandom(16).hex()}"
        temp_path, destination = update_seed(db, JOB_ID, expected, marker)

        app = subprocess.Popen(
            [support.BIN],
            env=support.app_env(home, None),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        completed = reattach.wait_job(db, JOB_ID, lambda job: job.get("state") == "completed", timeout=60.0)
        output = Path(completed["destination"])
        output_bytes = output.read_bytes()
        jobs = [job for job in reattach.read_jobs(db) if job.get("id") == JOB_ID]
        requests = proxy.requests_snapshot()
        assert output == Path(destination), (output, destination)
        assert output_bytes == expected, (len(output_bytes), len(expected))
        assert hashlib.sha256(output_bytes).hexdigest() == expected_hash
        assert not Path(temp_path).exists()
        assert len(jobs) == 1
        assert jobs[0].get("destinationReservation") is None, jobs[0]
        assert output.read_bytes() != marker.encode()
        assert proxy.snapshot().get("/changed.bin") == 1, proxy.snapshot()
        assert len(requests) == 1 and requests[0]["range"] == "bytes=0-0", requests
        print(f"STARTUP-RESERVATION-RECOVERY: PASS (job={JOB_ID}, destination={output.name}, sha256={expected_hash})", flush=True)
        print(f"STARTUP-RESERVATION-REQUESTS: {requests}", flush=True)
        print(f"E2E-ROOT: {home}", flush=True)
        return 0
    finally:
        if app is not None:
            support.terminate_only(app, "startup reservation app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "startup reservation fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "startup reservation Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STARTUP-RESERVATION-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
