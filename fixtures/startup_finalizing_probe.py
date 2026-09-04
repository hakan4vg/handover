#!/usr/bin/env python3
"""Real-binary proof for persisted finalizing media recovery at startup."""

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

JOB_ID = "startup-finalizing-recovery"


def seed_finalizing_job(db: str, home: str, proxy_base: str, fragments: dict[str, bytes]) -> str:
    temp = support.seed_job(db, home, proxy_base, JOB_ID, "startup-finalizing.mp4", fragments)
    segment_dir = Path(temp + ".segments")
    for global_index, (_kind, path) in enumerate(support.SEGMENTS):
        track = 0 if global_index < 4 else 1
        local_index = global_index if track == 0 else global_index - 4
        segment_path = segment_dir / f"{track:02}" / f"{local_index:08}.part"
        segment_path.parent.mkdir(parents=True, exist_ok=True)
        segment_path.write_bytes(fragments[path])
    total_bytes = sum(len(fragments[path]) for _kind, path in support.SEGMENTS)
    with sqlite3.connect(db) as connection:
        (payload,) = connection.execute("SELECT payload FROM jobs WHERE id = ?", (JOB_ID,)).fetchone()
        job = json.loads(payload)
        job.update({
            "state": "finalizing",
            "progress": 100.0,
            "downloaded": total_bytes,
            "connections": 0,
            "eta": "Finalizing",
            "segments": {"completed": 6, "total": 6, "identity": support.fnv_segment_identity(proxy_base)},
        })
        connection.execute("UPDATE jobs SET payload = ? WHERE id = ?", (json.dumps(job), JOB_ID))
        connection.commit()
    return temp


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-startup-finalizing-")
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
        support.wait_http(f"http://127.0.0.1:{upstream_port}/dash/manifest.mpd")
        proxy = support.DashProxy(upstream_port)
        proxy_base = f"http://127.0.0.1:{proxy.port}"
        fragments = {}
        for _kind, path in support.SEGMENTS:
            with urlopen(f"{proxy_base}{path}", timeout=30) as response:
                fragments[path] = response.read()
        reference, reference_hash = support.make_reference(root, fragments)
        proxy.reset_counts()

        reattach.boot_and_stop(root)
        db = support.wait_db(root)
        temp_path = seed_finalizing_job(db, root, proxy_base, fragments)
        app = subprocess.Popen(
            [support.BIN],
            env=support.app_env(root),
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
        counts = proxy.counts_copy()
        assert output_hash == reference_hash, (output_hash, reference_hash)
        assert counts.get("/dash/manifest.mpd") == 1, counts
        for _kind, path in support.SEGMENTS:
            assert counts.get(path, 0) == 0, counts
        assert len([job for job in reattach.read_jobs(db) if job.get("id") == JOB_ID]) == 1
        assert not Path(temp_path).exists()
        assert not Path(temp_path + ".segments").exists()
        print(f"STARTUP-FINALIZING-RECOVERY: PASS (job={JOB_ID}, sha256={output_hash})", flush=True)
        print(f"STARTUP-FINALIZING-REQUESTS: {counts}", flush=True)
        print(f"REFERENCE: {reference} sha256={reference_hash}", flush=True)
        print(f"E2E-ROOT: {root}", flush=True)
        return 0
    finally:
        if app is not None:
            support.terminate_only(app, "startup finalizing app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "startup finalizing fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "startup finalizing Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STARTUP-FINALIZING-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
