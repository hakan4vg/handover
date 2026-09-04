#!/usr/bin/env python3
"""Real-binary proof for explicit replace-mode destination collisions."""

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


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for job in reattach.read_jobs(db):
            if job.get("source") == source:
                return job
        time.sleep(0.1)
    raise RuntimeError(f"capture row did not appear: {reattach.read_jobs(db)}")


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-replace-commit-")
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    xvfb = None
    upstream = None
    app = None
    client = None
    try:
        xvfb, display = reattach.start_xvfb()
        reattach.support.DISPLAY = display
        upstream_port = reattach.support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, reattach.FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(reattach.FIXTURE_SERVER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        source = f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1&replace=1"
        reattach.support.wait_http(source)
        with urlopen(source) as response:
            expected = response.read()
        assert len(expected) == TOTAL

        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
        reattach.seed_job(db, home, "http://127.0.0.1:9/unused.bin", "replace-setup", "setup.bin", expected, '"unused"')
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
        reattach.send_capture(home, source, "replace.bin")
        captured = wait_source_job(db, source)
        job_id = captured["id"]
        reattach.wait_job(db, job_id, lambda job: job.get("state") == "finalizing", timeout=60.0)
        destination = str(Path(home) / "Downloads" / "replace.bin")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"OLD-DESINATION")
        reattach.support.invoke(client, "update_settings", {"patch": {"collisionBehavior": "replace"}})
        reattach.support.invoke(client, "commit_provisional", {"id": job_id, "input": {"name": "replace.bin", "destination": destination}})
        completed = reattach.wait_job(db, job_id, lambda job: job.get("state") == "completed", timeout=60.0)
        assert completed["destination"] == destination
        assert Path(destination).read_bytes() == expected
        output_hash = hashlib.sha256(Path(destination).read_bytes()).hexdigest()
        print(f"REPLACE-COLLISION: PASS (job={job_id}, destination={destination}, sha256={output_hash})", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            reattach.support.terminate_only(app, "replace app")
        if upstream is not None:
            reattach.support.terminate_only(upstream, "replace fixture")
        if xvfb is not None:
            reattach.support.terminate_only(xvfb, "replace Xvfb")


if __name__ == "__main__":
    raise SystemExit(main())
