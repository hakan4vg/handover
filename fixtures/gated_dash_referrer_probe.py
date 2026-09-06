#!/usr/bin/env python3
"""Resident proof: Referer replay composes with the DASH pipeline.

SPEC §5.1 + §16: the fixture's /dash/gated.mpd, gv-init.mp4, and every
gv-N.m4s 403 without a same-host Referer (real fMP4 bytes under gated
names). Phase A (red) creates the media job WITHOUT page context and must
fail honestly; phase B (green) replays the capture page and must complete
with output byte-identical to an ffmpeg `-c copy` mux of the fetched track
(the resident's exact mux invocation). Mirrors gated_media_referrer_probe
for the last transfer family.
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
FRAGMENTS = ["gv-init.mp4", "gv-0.m4s", "gv-1.m4s", "gv-2.m4s"]


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
        current = read_jobs(db)
        if current:
            last = current[-1]
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


def make_reference(root: str, fragments: dict[str, bytes]) -> tuple[str, str]:
    track = os.path.join(root, "dash-reference-video.track")
    output = os.path.join(root, "dash-reference.mp4")
    with open(track, "wb") as handle:
        for name in FRAGMENTS:
            handle.write(fragments[name])
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", track, "-map", "0:0", "-c", "copy", output],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
    return output, hashlib.sha256(open(output, "rb").read()).hexdigest()


def main() -> int:
    root = tempfile.mkdtemp(prefix="dm-gated-dash-")
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
        support.wait_http(f"{base}/dash/manifest.mpd")
        page_url = f"{base}/page/video.html"
        source = f"{base}/dash/gated.mpd"
        fragments = {name: fetch(f"{base}/dash/{name}", page_url) for name in FRAGMENTS}
        _reference, reference_hash = make_reference(root, fragments)
        reference_size = os.path.getsize(os.path.join(root, "dash-reference.mp4"))
        print(f"GATED-DASH-REFERENCE: fragments=4 bytes={reference_size} sha256={reference_hash}", flush=True)
        inspector_port = support.free_port()
        app_log = open(os.path.join(root, "app.log"), "wb")
        app = subprocess.Popen(
            [BIN], env=support.app_env(root, inspector_port),
            stdout=app_log, stderr=subprocess.STDOUT,
        )
        app_log.close()
        print(f"GATED-DASH-APP: display={support.DISPLAY} inspector={inspector_port}", flush=True)
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
        support.invoke(client, "create_provisional", {"input": {"source": source, "name": "gated-dash-red.mp4", "media": True}})
        red_id = wait_source_job(db, source)["id"]
        red = wait_job(db, red_id, lambda job: job.get("state") in ("completed", "failed"))
        print("GATED-DASH-RED:", json.dumps({"state": red.get("state"), "error": red.get("error")}, sort_keys=True), flush=True)
        assert red.get("state") == "failed" and "403" in str(red.get("error")), red
        # Phase B (green): replay the capture page — full DASH VOD completes.
        support.invoke(client, "create_provisional", {"input": {"source": source, "name": "gated-dash.mp4", "media": True, "pageUrl": page_url}})
        green_id = [job for job in read_jobs(db) if job.get("source") == source and job.get("id") != red_id][-1]["id"]
        ready = wait_job(db, green_id, lambda job: job.get("state") == "finalizing" and (job.get("segments") or {}).get("completed") == 4)
        print("GATED-DASH-READY:", json.dumps({"segments": ready.get("segments")}, sort_keys=True), flush=True)
        destination = os.path.join(root, "Downloads", "gated-dash.mp4")
        support.invoke(client, "commit_provisional", {"id": green_id, "input": {"name": "gated-dash.mp4", "destination": destination}})
        done = wait_job(db, green_id, lambda job: job.get("state") == "completed")
        assert done.get("provisional") is False, done
        output = Path(destination).read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert len(output) == reference_size and output_hash == reference_hash, (len(output), output_hash)
        print(f"GATED-DASH: PASS (output_bytes={len(output)}, output_sha256={output_hash}, segments=4)", flush=True)
        print("GATED-DASH-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "gated-dash app")
        if server is not None:
            support.terminate_only(server, "gated-dash fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "gated-dash Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"GATED-DASH-PROBE: FAIL: {error}", flush=True)
        raise
