#!/usr/bin/env python3
"""Regression for preserving an existing replace target when commit cannot move."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

import replace_commit_probe as replace


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-replace-failure-")
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    xvfb = None
    upstream = None
    app = None
    client = None
    app_log = None
    try:
        xvfb, display = replace.reattach.start_xvfb()
        setattr(replace.reattach, "DISPLAY", display)
        replace.reattach.support.DISPLAY = display
        upstream_port = replace.reattach.support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, replace.reattach.FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(replace.reattach.FIXTURE_SERVER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        source = f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1&replace-failure=1"
        replace.reattach.support.wait_http(source)
        with urlopen(source) as response:
            expected = response.read()
        replace.reattach.boot_and_stop(home)
        db = replace.reattach.db_path(home)
        replace.reattach.seed_job(
            db,
            home,
            "http://127.0.0.1:9/unused.bin",
            "replace-failure-setup",
            "setup.bin",
            expected,
            '"unused"',
        )
        inspector_port = replace.reattach.support.free_port()
        app_log = open(Path(home) / "app.log", "w", encoding="utf-8")
        app = subprocess.Popen(
            [replace.reattach.BIN],
            env=replace.reattach.support.app_env(home, inspector_port),
            stdout=app_log,
            stderr=subprocess.STDOUT,
        )
        replace.reattach.wait_db(db)
        try:
            client = replace.reattach.support.wait_inspector(inspector_port)
        except Exception:
            app_log.flush()
            raise RuntimeError(f"WebKit inspector startup failed; app log:\n{Path(home, 'app.log').read_text(errors='replace')}")
        replace.reattach.support.wait_tauri(client)
        replace.reattach.send_capture(home, source, "replace-failure.bin")
        job = replace.wait_source_job(db, source)
        job_id = job["id"]
        ready = replace.reattach.wait_job(db, job_id, lambda row: row.get("state") == "finalizing", timeout=60.0)
        temp_path = ready["tempPath"]
        destination = str(Path(home) / "Downloads" / "replace-failure.bin")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        old = b"OLD-DESTINATION-MUST-SURVIVE"
        Path(destination).write_bytes(old)
        os.unlink(temp_path)
        replace.reattach.support.invoke(client, "update_settings", {"patch": {"collisionBehavior": "replace"}})
        replace.reattach.support.invoke(client, "commit_provisional", {"id": job_id, "input": {"name": "replace-failure.bin", "destination": destination}})
        failed = replace.reattach.wait_job(db, job_id, lambda row: row.get("state") == "failed", timeout=60.0)
        assert Path(destination).read_bytes() == old, (failed, destination)
        print(f"REPLACE-FAILURE-CLEANUP: PASS (job={job_id}, preserved={destination}, error={failed.get('error')!r})", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            replace.reattach.support.terminate_only(app, "replace failure app")
        if app_log is not None:
            app_log.close()
        if upstream is not None:
            replace.reattach.support.terminate_only(upstream, "replace failure fixture")
        if xvfb is not None:
            replace.reattach.support.terminate_only(xvfb, "replace failure Xvfb")


if __name__ == "__main__":
    raise SystemExit(main())
