#!/usr/bin/env python3
"""Prove media finalization failure preserves parts and retry reuses them.

The real application first runs with an isolated PATH containing no ffmpeg. It
captures a finite HLS fMP4 video playlist, commits the provisional job, and
checks that finalization fails without creating an output or refetching parts.
A real ffmpeg symlink is then added to that same PATH and the failed managed job
is retried. The retry must finalize from the preserved four parts, with no media
fragment fetched a second time.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

import hls_fmp4_probe as hls
import segmented_restart_probe as support


def wait_failed(db: str, job_id: str, timeout: float = 45.0) -> dict:
    return hls.wait_job(
        db,
        job_id,
        lambda job: job.get("state") == "failed"
        and "FFmpeg is required" in (job.get("error") or ""),
        timeout,
    )


def wait_completed(db: str, job_id: str, timeout: float = 60.0) -> dict:
    return hls.wait_job(db, job_id, lambda job: job.get("state") == "completed", timeout)


def make_video_reference(root: str, fragments: dict[str, bytes]) -> str:
    video = Path(root) / "video-reference.track"
    output = Path(root) / "video-reference.mp4"
    with video.open("wb") as handle:
        for path in hls.SEGMENTS[:4]:
            handle.write(fragments[path])
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-c", "copy", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required by the reference assembly")
    root = tempfile.mkdtemp(prefix="dm-missing-ffmpeg-")
    no_ffmpeg = Path(root) / "no-ffmpeg"
    no_ffmpeg.mkdir()
    xvfb = None
    upstream = None
    proxy = None
    app = None
    client = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        upstream_port = support.free_port()
        upstream = subprocess.Popen(
            [sys.executable, hls.FIXTURE_SERVER, "--port", str(upstream_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        support.wait_http(f"http://127.0.0.1:{upstream_port}/dash/manifest.mpd")
        proxy = hls.HlsProxy(upstream_port)
        proxy_base = f"http://127.0.0.1:{proxy.port}"
        source = f"{proxy_base}/hls/video.m3u8"
        fragments = {}
        for path in hls.SEGMENTS:
            with urlopen(f"{proxy_base}{path}", timeout=30) as response:
                fragments[path] = response.read()
        reference_hash = make_video_reference(root, fragments)
        proxy.reset()

        inspector_port = support.free_port()
        env = support.app_env(root, inspector_port)
        env["PATH"] = str(no_ffmpeg)
        app = subprocess.Popen(
            [support.BIN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        client = support.wait_inspector(inspector_port)
        support.wait_tauri(client)
        support.invoke(
            client,
            "create_provisional",
            {"input": {"source": source, "name": "missing-ffmpeg.mp4", "media": True, "maxConnections": 2}},
        )
        db = hls.db_path(root)
        created = hls.wait_source_job(db, source)
        job_id = created["id"]
        ready = hls.wait_job(
            db,
            job_id,
            lambda job: job.get("state") == "finalizing"
            and job.get("segments", {}).get("completed") == 4,
        )
        destination = str(Path(root) / "Downloads" / "missing-ffmpeg.mp4")
        support.invoke(
            client,
            "commit_provisional",
            {"id": job_id, "input": {"name": "missing-ffmpeg.mp4", "destination": destination, "maxConnections": 2}},
        )
        failed = wait_failed(db, job_id)
        temp_path = failed["tempPath"]
        segment_dir = Path(temp_path + ".segments")
        preserved = sorted(segment_dir.rglob("*.part"))
        counts_after_failure = proxy.copy_counts()
        assert len(preserved) == 4, preserved
        assert not os.path.exists(destination), destination
        assert all(counts_after_failure.get(path) == 1 for path in hls.SEGMENTS[:4]), counts_after_failure
        print(
            f"MISSING-FFMPEG: PASS (state=failed, parts={len(preserved)}, "
            f"error={failed['error']})",
            flush=True,
        )

        os.symlink(ffmpeg, no_ffmpeg / "ffmpeg")
        support.invoke(client, "retry_job", {"id": job_id})
        completed = wait_completed(db, job_id)
        output = completed["destination"]
        output_hash = hashlib.sha256(Path(output).read_bytes()).hexdigest()
        counts_after_retry = proxy.copy_counts()
        assert output_hash == reference_hash, (output_hash, reference_hash)
        assert all(counts_after_retry.get(path) == 1 for path in hls.SEGMENTS[:4]), counts_after_retry
        assert not segment_dir.exists(), segment_dir
        print(
            f"MISSING-FFMPEG-RETRY: PASS (output={len(Path(output).read_bytes())} bytes, "
            f"sha256={output_hash}, segment_refetches=0)",
            flush=True,
        )
        print(f"REQUESTS: {counts_after_retry}", flush=True)
        print(f"E2E-ROOT: {root}", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "missing-ffmpeg app")
        if proxy is not None:
            proxy.stop()
        if upstream is not None:
            support.terminate_only(upstream, "missing-ffmpeg fixture")
        if xvfb is not None:
            support.terminate_only(xvfb, "missing-ffmpeg Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MISSING-FFMPEG-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
