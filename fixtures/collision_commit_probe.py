#!/usr/bin/env python3
"""Real-binary proof that concurrent commits reserve collision-safe paths."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import reattach_probe as reattach

TOTAL = reattach.TOTAL


def wait_source_jobs(db: str, sources: list[str], timeout: float = 30.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = [job for job in reattach.read_jobs(db) if job.get("source") in sources]
        if len(jobs) == len(sources):
            return [next(job for job in jobs if job.get("source") == source) for source in sources]
        time.sleep(0.1)
    raise RuntimeError(f"capture rows did not appear: {reattach.read_jobs(db)}")


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-collision-commit-")
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    xvfb = None
    upstream = None
    app = None
    client = None
    try:
        xvfb, display = reattach.start_xvfb()
        setattr(reattach, "DISPLAY", display)
        reattach.support.DISPLAY = display
        upstream_port = reattach.support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, reattach.FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(reattach.FIXTURE_SERVER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{upstream_port}/changed.bin"
        sources = [base + "?variant=1&capture=one", base + "?variant=2&capture=two"]
        reattach.support.wait_http(sources[0])
        with urlopen(sources[0]) as response:
            data_a = response.read()
        with urlopen(sources[1]) as response:
            data_b = response.read()
        assert len(data_a) == TOTAL and len(data_b) == TOTAL

        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
        reattach.seed_job(db, home, "http://127.0.0.1:9/unused.bin", "collision-setup", "setup.bin", data_a, '"unused"')

        inspector_port = reattach.support.free_port()
        app_log_path = Path(home) / "app.log"
        app_log = open(app_log_path, "wb")
        app = subprocess.Popen(
            [reattach.BIN],
            env=reattach.support.app_env(home, inspector_port),
            stdout=app_log,
            stderr=subprocess.STDOUT,
        )
        app_log.close()
        reattach.wait_db(db)
        try:
            client = reattach.support.wait_inspector(inspector_port)
            reattach.support.wait_tauri(client)
        except Exception:
            print(f"APP: poll={app.poll()}", flush=True)
            print(f"APP-LOG: {app_log_path.read_text(errors='replace')}", flush=True)
            raise
        for index, source in enumerate(sources, start=1):
            reattach.send_capture(home, source, f"capture-{index}.bin")
        captured = wait_source_jobs(db, sources)
        job_ids = [job["id"] for job in captured]
        for job_id in job_ids:
            reattach.wait_job(db, job_id, lambda job: job.get("state") == "finalizing", timeout=60.0)

        destination = str(Path(home) / "Downloads" / "same.bin")
        calls = ",".join(
            "window.__TAURI_INTERNALS__.invoke('commit_provisional', %s)" % json.dumps({"id": job_id, "input": {"name": "same.bin", "destination": destination}})
            for job_id in job_ids
        )
        result = client.evaluate(f"Promise.all([{calls}])")
        print(f"COMMIT-RESULT: {result!r}", flush=True)
        completed = [reattach.wait_job(db, job_id, lambda job: job.get("state") == "completed", timeout=60.0) for job_id in job_ids]
        destinations = [job["destination"] for job in completed]
        assert len(set(destinations)) == 2, destinations
        assert sorted(Path(path).name for path in destinations) == ["same (1).bin", "same.bin"], destinations
        hashes = [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in destinations]
        expected_hashes = [hashlib.sha256(data_a).hexdigest(), hashlib.sha256(data_b).hexdigest()]
        assert hashes == expected_hashes, (hashes, expected_hashes, destinations)
        print(f"COLLISION-RESERVATION: PASS (jobs={job_ids}, destinations={destinations}, hashes={hashes})", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            reattach.support.terminate_only(app, "collision app")
        if upstream is not None:
            reattach.support.terminate_only(upstream, "collision fixture")
        if xvfb is not None:
            reattach.support.terminate_only(xvfb, "collision Xvfb")


if __name__ == "__main__":
    raise SystemExit(main())
