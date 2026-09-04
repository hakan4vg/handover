#!/usr/bin/env python3
"""Real-binary proof that pause/cancel after a durable move wins the race."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import reattach_probe as reattach

COPY_SIZE = 256 * 1024 * 1024
JOB_NAME = "cancel-race.bin"


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = [job for job in reattach.read_jobs(db) if job.get("source") == source]
        if jobs:
            return jobs[0]
        time.sleep(0.1)
    raise RuntimeError(f"capture row did not appear: {reattach.read_jobs(db)}")


def write_large_temp(path: str) -> str:
    block = bytes((index * 29 + 7) % 256 for index in range(1024 * 1024))
    digest = hashlib.sha256()
    remaining = COPY_SIZE
    with open(path, "wb") as handle:
        while remaining:
            chunk = block[: min(len(block), remaining)]
            handle.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-cancel-during-move-home-")
    output_root = tempfile.mkdtemp(prefix="dm-cancel-during-move-output-", dir="/srv/repos/downloadmanager")
    xvfb = None
    upstream = None
    app = None
    client = None
    commit_thread = None
    commit_result: list[object] = []
    paused = False
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
        source = f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1&cancel-race=1"
        reattach.support.wait_http(source)
        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
        inspector_port = reattach.support.free_port()
        app = subprocess.Popen(
            [reattach.BIN],
            env=reattach.support.app_env(home, inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        reattach.wait_db(db)
        client = reattach.support.wait_inspector(inspector_port)
        reattach.support.wait_tauri(client)
        reattach.send_capture(home, source, JOB_NAME)
        job = wait_source_job(db, source)
        job_id = job["id"]
        job = reattach.wait_job(db, job_id, lambda current: current.get("state") == "finalizing", timeout=60.0)
        temp_path = job["tempPath"]
        expected_hash = write_large_temp(temp_path)
        destination = str(Path(output_root) / JOB_NAME)
        payload = {"id": job_id, "input": {"name": JOB_NAME, "destination": destination}}

        def commit() -> None:
            try:
                commit_result.append(client.evaluate("window.__TAURI_INTERNALS__.invoke('commit_provisional', %s)" % json.dumps(payload)))
            except BaseException as error:  # retain the real failure for the assertion below
                commit_result.append(error)

        commit_thread = threading.Thread(target=commit, daemon=True)
        commit_thread.start()
        deadline = time.time() + 30.0
        while time.time() < deadline:
            output = Path(destination)
            if output.exists() and output.stat().st_size > 1024 * 1024:
                client.evaluate("window.__TAURI_INTERNALS__.invoke('pause_job', %s)" % json.dumps({"id": job_id}))
                paused = True
                break
            time.sleep(0.001)
        assert paused, f"fallback copy did not become observable; destination={destination}"
        commit_thread.join(timeout=60.0)
        assert not commit_thread.is_alive(), "commit did not finish"
        assert not isinstance(commit_result[0], BaseException), commit_result

        completed = reattach.wait_job(db, job_id, lambda current: current.get("state") == "completed", timeout=60.0)
        output = Path(completed["destination"])
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        assert output == Path(destination), (output, destination)
        assert output.stat().st_size == COPY_SIZE
        assert digest == expected_hash, (digest, expected_hash)
        assert not Path(temp_path).exists()
        assert completed.get("destinationReservation") is None, completed
        assert len([current for current in reattach.read_jobs(db) if current.get("id") == job_id]) == 1
        print(f"MOVE-CANCEL-RACE: PASS (job={job_id}, state={completed['state']}, bytes={COPY_SIZE}, sha256={digest})", flush=True)
        print(f"MOVE-CANCEL-RESULT: {commit_result[0]!r}", flush=True)
        print(f"E2E-ROOT: {home}", flush=True)
        print(f"OUTPUT-ROOT: {output_root}", flush=True)
        return 0
    finally:
        if commit_thread is not None and commit_thread.is_alive():
            commit_thread.join(timeout=5.0)
        if client is not None:
            client.close()
        if app is not None:
            reattach.support.terminate_only(app, "move cancel app")
        if upstream is not None:
            reattach.support.terminate_only(upstream, "move cancel fixture")
        if xvfb is not None:
            reattach.support.terminate_only(xvfb, "move cancel Xvfb")
        try:
            import shutil
            shutil.rmtree(output_root)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MOVE-CANCEL-RACE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
