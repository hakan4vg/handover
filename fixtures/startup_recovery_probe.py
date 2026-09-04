#!/usr/bin/env python3
"""Real-binary proof for automatic startup recovery of a committed range job."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import reattach_probe as reattach
import segmented_restart_probe as support

PARTIAL = 64 * 1024
TOTAL = 256 * 1024
JOB_ID = "startup-range-recovery"


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-startup-recovery-")
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
            "startup-recovered.bin",
            expected,
            '"v1"',
            state="downloading",
        )
        assert Path(temp_path).stat().st_size == TOTAL
        app = subprocess.Popen(
            [support.BIN],
            env=support.app_env(home, None),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        completed = reattach.wait_job(
            db,
            JOB_ID,
            lambda job: job.get("state") == "completed",
            timeout=60.0,
        )
        output = Path(completed["destination"])
        output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        requests = proxy.requests_snapshot()
        assert output_hash == expected_hash, (output_hash, expected_hash)
        assert len([job for job in reattach.read_jobs(db) if job.get("id") == JOB_ID]) == 1
        assert not Path(temp_path).exists()
        assert proxy.snapshot().get("/changed.bin") == 2, proxy.snapshot()
        reattach.assert_range_requests(requests, "variant=1", '"v1"', PARTIAL)
        print(f"STARTUP-AUTO-RECOVERY: PASS (job={JOB_ID}, sha256={output_hash})", flush=True)
        print(f"STARTUP-REQUESTS: {requests}", flush=True)
        print(f"E2E-ROOT: {home}", flush=True)
        return 0
    finally:
        if app is not None:
            support.terminate_only(app, "startup recovery app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "startup recovery fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "startup recovery Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STARTUP-RECOVERY-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
