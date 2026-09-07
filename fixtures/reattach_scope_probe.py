#!/usr/bin/env python3
"""Real-binary proof that targeted reattach survives an incompatible capture."""

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

JOB_ID = "reattach-targeted-scope"


def wait_for_job(db: str, predicate, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        for job in reattach.read_jobs(db):
            if predicate(job):
                return job
        last = reattach.read_jobs(db)
        time.sleep(0.1)
    raise RuntimeError(f"job did not reach expected state: {last}")


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-reattach-scope-")
    xvfb = None
    upstream = None
    proxy = None
    app = None
    client = None
    try:
        xvfb, display = reattach.start_xvfb()
        reattach.DISPLAY = display
        reattach.support.DISPLAY = display
        upstream_port = reattach.support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, reattach.FIXTURE_SERVER, "--port", str(upstream_port)],
            cwd=os.path.dirname(reattach.FIXTURE_SERVER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        reattach.support.wait_http(f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1")
        proxy = reattach.CountingProxy(upstream_port)
        base = f"http://127.0.0.1:{proxy.port}"
        target_source = f"{base}/changed.bin?variant=1"
        renewed_source = f"{base}/changed.bin?variant=1&renewed=1"
        incompatible_source = f"{base}/other.bin?unrelated=1"
        with urlopen(target_source) as response:
            expected = response.read()
        expected_hash = hashlib.sha256(expected).hexdigest()
        proxy.reset()

        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
        temp_path = reattach.seed_job(
            db,
            home,
            target_source,
            JOB_ID,
            "reattach-targeted.bin",
            expected,
            '"v1"',
        )
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
        try:
            client = reattach.support.wait_inspector(inspector_port)
            reattach.support.wait_tauri(client)
        except Exception:
            print(f"APP: poll={app.poll()}", flush=True)
            if app_log_path.exists():
                print(f"APP-LOG: {app_log_path.read_text(errors='replace')}", flush=True)
            raise

        reattach.support.invoke(client, "reattach_job", {"id": JOB_ID})
        armed = reattach.wait_job(db, JOB_ID, lambda job: job.get("state") == "pending")
        assert armed.get("provisional") is False, armed

        reattach.send_capture(home, incompatible_source, "unrelated.bin")
        unrelated = wait_for_job(
            db,
            lambda job: job.get("source") == incompatible_source,
            timeout=30.0,
        )
        unrelated = reattach.wait_job(db, unrelated["id"], lambda job: job.get("state") == "failed")
        target_after_incompatible = reattach.wait_job(db, JOB_ID, lambda job: job.get("state") == "pending")
        assert target_after_incompatible.get("source") == target_source, target_after_incompatible
        assert len([job for job in reattach.read_jobs(db) if job.get("id") == JOB_ID]) == 1
        assert unrelated.get("provisional") is True, unrelated

        reattach.send_capture(home, renewed_source, "reattach-targeted.bin", "fixture=reattach-renewed")
        completed = reattach.wait_job(db, JOB_ID, lambda job: job.get("state") == "completed")
        assert completed.get("postBody") == "fixture=reattach-renewed", completed
        output = Path(completed["destination"])
        output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        requests = proxy.requests_snapshot()
        assert output_hash == expected_hash, (output_hash, expected_hash)
        assert len([job for job in reattach.read_jobs(db) if job.get("id") == JOB_ID]) == 1
        assert not Path(temp_path).exists()
        target_requests = [request for request in requests if request["path"] == "/changed.bin"]
        reattach.assert_range_requests(target_requests, "variant=1&renewed=1", '"v1"', reattach.PARTIAL)
        print(f"REATTACH-INCOMPATIBLE-ISOLATED: PASS (unrelated job={unrelated['id']}, target stayed armed)", flush=True)
        print(f"REATTACH-COMPATIBLE-AFTERWARD: PASS (job={JOB_ID}, sha256={output_hash})", flush=True)
        print(f"REQUESTS: {requests}", flush=True)
        print(f"E2E-ROOT: {home}", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            reattach.support.terminate_only(app, "reattach scope app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            reattach.support.terminate_only(upstream, "reattach scope fixture")
        if xvfb is not None:
            reattach.support.terminate_only(xvfb, "reattach scope Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"REATTACH-SCOPE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
