#!/usr/bin/env python3
"""Real-binary proof that managed rename-mode commits do not overwrite files."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

import reattach_probe as reattach


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-managed-collision-")
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
        source = f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1&managed-collision=1"
        reattach.support.wait_http(source)
        with urlopen(source) as response:
            expected = response.read()
        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
        job_id = "managed-collision"
        destination = Path(home) / "Downloads" / "managed.bin"
        reattach.seed_job(db, home, source, job_id, "managed.bin", expected, '"variant-1"')
        destination.parent.mkdir(parents=True, exist_ok=True)
        old = b"OLD-MANAGED-DOWNLOAD"
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
        destination.write_bytes(old)
        reattach.support.invoke(client, "resume_job", {"id": job_id})
        completed = reattach.wait_job(db, job_id, lambda job: job.get("state") == "completed", timeout=60.0)
        renamed = Path(home) / "Downloads" / "managed (1).bin"
        assert completed["destination"] == str(renamed), (completed, destination, renamed)
        assert destination.read_bytes() == old
        assert renamed.read_bytes() == expected
        output_hash = hashlib.sha256(renamed.read_bytes()).hexdigest()
        print(f"MANAGED-RENAME-COLLISION: PASS (job={job_id}, destination={renamed}, sha256={output_hash})", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            reattach.support.terminate_only(app, "managed collision app")
        if upstream is not None:
            reattach.support.terminate_only(upstream, "managed collision fixture")
        if xvfb is not None:
            reattach.support.terminate_only(xvfb, "managed collision Xvfb")


if __name__ == "__main__":
    raise SystemExit(main())
