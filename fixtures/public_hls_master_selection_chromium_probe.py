#!/usr/bin/env python3
"""Public hls.js multivariant-master and active-rendition acceptance probe."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urljoin

import adaptive_hls_quality_probe as probe

MASTER = "https://demo-public.gvideo.io/videos/2675_HCzdHTj79iSt3wiW/master.m3u8"
PAGE = "https://hlsjs.video-dev.org/demo/?src=" + quote(MASTER, safe="")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Referer": PAGE,
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def playlist_urls(source: str, body: bytes) -> list[str]:
    text = body.decode("utf-8")
    return [
        urljoin(source, line.strip())
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def attr(line: str, name: str) -> str | None:
    match = re.search(rf'(?:^|,){re.escape(name)}=(?:"([^"]*)"|([^,]*))', line)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def independent_reference(_client, selected_manifest: str) -> dict:
    master_body = fetch(MASTER).decode("utf-8")
    audio_manifest = None
    for line in master_body.splitlines():
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line.upper():
            uri = attr(line, "URI")
            if uri and (attr(line, "DEFAULT") or "").upper() == "YES":
                audio_manifest = urljoin(MASTER, uri)
                break
            if uri and audio_manifest is None:
                audio_manifest = urljoin(MASTER, uri)
    if audio_manifest is None:
        raise RuntimeError("public master had no audio rendition")

    root = Path(tempfile.mkdtemp(prefix="dm-adaptive-hls-gvideo-reference-", dir="/srv/backup-export"))
    video_track = root / "video.ts"
    audio_track = root / "audio.ts"
    output = root / "reference.ts"
    video_parts = playlist_urls(selected_manifest, fetch(selected_manifest))
    audio_parts = playlist_urls(audio_manifest, fetch(audio_manifest))
    if not video_parts or not audio_parts:
        raise RuntimeError(f"empty public rendition: video={len(video_parts)} audio={len(audio_parts)}")
    with video_track.open("wb") as handle:
        for part in video_parts:
            handle.write(fetch(part))
    with audio_track.open("wb") as handle:
        for part in audio_parts:
            handle.write(fetch(part))
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_track), "-i", str(audio_track),
            "-map", "0:0", "-map", "1:0", "-c", "copy", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"independent FFmpeg reference failed: {result.stderr.strip()}")
    data = output.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    print(
        "INDEPENDENT-REFERENCE-DETAIL:",
        json.dumps(
            {
                "root": str(root),
                "video_manifest": selected_manifest,
                "audio_manifest": audio_manifest,
                "video_segments": len(video_parts),
                "audio_segments": len(audio_parts),
                "size": len(data),
                "sha256": digest,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return {
        "manifest": selected_manifest,
        "segments": len(video_parts) + len(audio_parts),
        "size": len(data),
        "hash": digest,
    }


def wait_master_job(db: str, _source: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in probe.public.jobs(db) if job.get("source") == MASTER]
        if last:
            return last[-1]
        time.sleep(0.1)
    raise RuntimeError(f"capture did not create the public master job: {last}")


def main() -> int:
    probe.PAGE = PAGE
    probe.MASTER = MASTER
    probe.public.wait_job = wait_master_job
    probe.browser_variant_reference = independent_reference
    return probe.main()


if __name__ == "__main__":
    raise SystemExit(main())
