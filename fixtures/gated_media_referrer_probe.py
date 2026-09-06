#!/usr/bin/env python3
"""Resident proof: Referer replay composes with the segmented media pipeline.

SPEC §5.1 + §16: the fixture's /hls/gated.m3u8 and every gseg segment 403
without a same-host Referer. Phase A (red) creates the media job WITHOUT
page context and must fail honestly; phase B (green) replays the capture
page and must complete with output byte-identical to the ordered segment
concatenation. Driven through the real binary's Inspector (create/commit),
no Chromium needed — the extension-to-media forwarding link is covered by
the ordinary-path E2E plus unit parse tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = support.BIN
FIXTURE_SERVER = support.FIXTURE_SERVER
SEGMENTS = [f"/hls/gseg{i}.ts" for i in range(6)]


def read_jobs(db: str) -> list[dict]:
    if not os.path.exists(db):
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT payload FROM jobs").fetchall()
    finally:
        con.close()
    return [json.loads(payload) for (payload,) in rows]


def wait_source_job(db: str, source: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        matches = [job for job in read_jobs(db) if job.get("source") == source]
        if matches:
            return matches[-1]
        if read_jobs(db):
            last = read_jobs(db)[-1]
        time.sleep(0.2)
    raise RuntimeError(f"no job for {source}: {last}")


def wait_job(db: str, job_id: str, predicate, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in read_jobs(db) if job.get("id") == job_id), None)
        if last is not None and predicate(last):
            return last
        time.sleep(0.2)
    raise RuntimeError(f"job {job_id} did not reach expected state: {last}")


def fetch(url: str, referer: str) -> bytes:
    with urlopen(Request(url, headers={"Referer": referer}), timeout=30) as response:
        return response.read()


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-gated-media-")
    xvfb = None
    server = None
    app = None
    client = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        port = support.free_port()
        server = subprocess.Popen(
            [sys.executable, FIXTURE_SERVER, "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        support.wait_http(f"{base}/hls/vod.m3u8")
        page_url = f"{base}/page/video.html"
        source = f"{base}/hls/gated.m3u8"
        # Byte reference: ordered segment concatenation (mpegts), fetched
        # with a same-host Referer exactly as the gate demands.
        parts = [fetch(f"{base}{path}", page_url) for path in SEGMENTS]
        reference = b"".join(parts)
        reference_hash = hashlib.sha256(reference).hexdigest()
        print(f"GATED-REFERENCE: segments=6 bytes={len(reference)} sha256={reference_hash}", flush=True)
        inspector_port = support.free_port()
        app_log = open(os.path.join(root, "app.log"), "wb")
        app = subprocess.Popen(
            [BIN], env=support.app_env(root, inspector_port),
            stdout=app_log, stderr=subprocess.STDOUT,
        )
        app_log.close()
        print(f"GATED-APP: display={support.DISPLAY} inspector={inspector_port}", flush=True)
        try:
            client = support.wait_inspector(inspector_port)
            support.wait_tauri(client)
        except Exception:
            print(f"APP: poll={app.poll()}", flush=True)
            log_path = os.path.join(root, "app.log")
            if os.path.exists(log_path):
                print(f"APP-LOG-TAIL: {open(log_path, encoding='utf-8', errors='replace').read()[-1500:]}", flush=True)
            raise
        db = support.db_path(root)
        # Phase A (red): no page context — the manifest fetch 403s honestly.
        support.invoke(client, "create_provisional", {"input": {"source": source, "name": "gated-red.ts", "media": True}})
        red_id = wait_source_job(db, source)["id"]
        red = wait_job(db, red_id, lambda job: job.get("state") in ("completed", "failed"))
        print("GATED-MEDIA-RED:", json.dumps({"state": red.get("state"), "error": red.get("error")}, sort_keys=True), flush=True)
        assert red.get("state") == "failed" and "403" in str(red.get("error")), red
        # Phase B (green): replay the capture page — full VOD completes.
        support.invoke(client, "create_provisional", {"input": {"source": source, "name": "gated-vod.ts", "media": True, "pageUrl": page_url}})
        green_id = [job for job in read_jobs(db) if job.get("source") == source and job.get("id") != red_id][-1]["id"]
        ready = wait_job(db, green_id, lambda job: job.get("state") == "finalizing" and (job.get("segments") or {}).get("completed") == 6)
        print("GATED-MEDIA-READY:", json.dumps({"segments": ready.get("segments")}, sort_keys=True), flush=True)
        destination = os.path.join(root, "Downloads", "gated-vod.ts")
        support.invoke(client, "commit_provisional", {"id": green_id, "input": {"name": "gated-vod.ts", "destination": destination}})
        done = wait_job(db, green_id, lambda job: job.get("state") == "completed")
        assert done.get("provisional") is False, done
        output = Path(destination).read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert len(output) == len(reference) and output_hash == reference_hash, (len(output), output_hash)
        print(f"GATED-MEDIA: PASS (output_bytes={len(output)}, output_sha256={output_hash}, segments=6)", flush=True)
        print("GATED-MEDIA-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "gated-media app")
        if server is not None:
            support.terminate_only(server, "gated-media fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "gated-media Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"GATED-MEDIA-PROBE: FAIL: {error}", flush=True)
        raise