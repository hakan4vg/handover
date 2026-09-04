#!/usr/bin/env python3
"""Real-binary proof that quick captures remain independently addressable."""

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


def wait_for_sources(db: str, sources: set[str], timeout: float = 30.0) -> list[dict]:
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        jobs = reattach.read_jobs(db)
        matching = [job for job in jobs if job.get("source") in sources]
        last = matching
        if len(matching) == len(sources) and all(job.get("state") == "finalizing" for job in matching):
            return matching
        time.sleep(0.1)
    raise RuntimeError(f"captures did not become independent finalizing jobs: {last}")


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-simultaneous-capture-")
    xvfb = None
    upstream = None
    proxy = None
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
        reattach.support.wait_http(f"http://127.0.0.1:{upstream_port}/changed.bin?variant=1")
        proxy = reattach.CountingProxy(upstream_port)
        base = f"http://127.0.0.1:{proxy.port}/changed.bin"
        sources = {
            f"{base}?variant=1&capture=one": 0x55,
            f"{base}?variant=2&capture=two": 0x56,
        }
        expected = {}
        for source in sources:
            with urlopen(source) as response:
                expected[source] = response.read()
        proxy.reset()

        reattach.boot_and_stop(home)
        db = reattach.db_path(home)
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
            if app_log_path.exists():
                print(f"APP-LOG: {app_log_path.read_text(errors='replace')}", flush=True)
            raise
        for index, source in enumerate(sources, start=1):
            reattach.send_capture(home, source, f"capture-{index}.bin")
        jobs = wait_for_sources(db, set(sources))
        jobs_by_source = {job["source"]: job for job in jobs}
        assert len(jobs_by_source) == 2
        assert len({job["id"] for job in jobs}) == 2
        assert all(job.get("provisional") is True for job in jobs), jobs
        for source, seed in sources.items():
            job = jobs_by_source[source]
            temp = Path(job["tempPath"])
            assert temp.exists(), temp
            assert hashlib.sha256(temp.read_bytes()).hexdigest() == hashlib.sha256(expected[source]).hexdigest()
            assert job["name"] in {"capture-1.bin", "capture-2.bin"}
        counts = proxy.snapshot()
        assert counts.get("/changed.bin") == 4, counts
        print(f"SIMULTANEOUS-CAPTURES: PASS (jobs={sorted(job['id'] for job in jobs)})", flush=True)
        print(f"CAPTURE-SOURCES: PASS ({sorted(jobs_by_source)})", flush=True)
        print(f"REQUESTS: {counts}", flush=True)
        print(f"E2E-ROOT: {home}", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            reattach.support.terminate_only(app, "simultaneous capture app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            reattach.support.terminate_only(upstream, "simultaneous capture fixture")
        if xvfb is not None:
            reattach.support.terminate_only(xvfb, "simultaneous capture Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SIMULTANEOUS-CAPTURE-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
