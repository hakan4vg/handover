#!/usr/bin/env python3
"""Real Chromium proof for a custom-element media player (mux-video).

The Media Chrome <mux-video> example plays an HLS VOD through a custom element.
The actual <video> lives inside mux-video's open shadow root; the page exposes
no light-DOM video/audio. This probe trusted-clicks the media-chrome Play
control, waits for playback, and captures through the resident application.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_chromium_probe as public
import cdp_drive
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://media-chrome.mux.dev/examples/vanilla/media-elements/mux-video.html"
UA = "Download Manager/0.1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def playlist_media_urls(playlist_url: str, body: str) -> tuple[list[str], str | None]:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    map_uri = None
    for line in lines:
        match = re.match(r'#EXT-X-MAP:.*URI="([^"]+)"', line)
        if match:
            map_uri = urljoin(playlist_url, match.group(1))
    segments = [urljoin(playlist_url, line) for line in lines if not line.startswith("#")]
    return segments, map_uri


def assemble_track(playlist_url: str) -> bytes:
    body = fetch_bytes(playlist_url).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in body:
        raise RuntimeError(f"expected a rendition playlist, got a master: {playlist_url}")
    if "#EXT-X-ENDLIST" not in body:
        raise RuntimeError(f"expected a finite VOD playlist: {playlist_url}")
    segments, map_uri = playlist_media_urls(playlist_url, body)
    if not segments:
        raise RuntimeError(f"playlist has no segments: {playlist_url}")
    parts = []
    if map_uri:
        parts.append(fetch_bytes(map_uri))
    for segment in segments:
        parts.append(fetch_bytes(segment))
    return b"".join(parts)


def master_tracks(master_url: str) -> list[str]:
    body = fetch_bytes(master_url).decode("utf-8", "replace")
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    video = None
    audio = None
    pending = False
    for line in lines:
        if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line.upper():
            match = re.search(r'URI="([^"]+)"', line)
            if match and audio is None:
                audio = urljoin(master_url, match.group(1))
        elif line.startswith("#EXT-X-STREAM-INF"):
            pending = True
        elif pending and not line.startswith("#"):
            video = urljoin(master_url, line)
            break
    if not video:
        raise RuntimeError(f"master has no video variant: {master_url}")
    return [video] + ([audio] if audio else [])


def ffmpeg_reference(track_paths: list[str], output: Path) -> None:
    if len(track_paths) == 1:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", track_paths[0], "-map", "0", "-c", "copy", str(output)]
    else:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for path in track_paths:
            command += ["-i", path]
        for index in range(len(track_paths)):
            command += ["-map", f"{index}:0"]
        command += ["-c", "copy", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg reference failed: {result.stderr[:500]}")


def build_reference(source: str, workdir: Path) -> dict:
    body = fetch_bytes(source).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in body:
        track_playlists = master_tracks(source)
    else:
        track_playlists = [source]
    tracks = []
    for index, playlist in enumerate(track_playlists):
        track_bytes = assemble_track(playlist)
        track_file = workdir / f"track-{index}.mux"
        track_file.write_bytes(track_bytes)
        tracks.append(str(track_file))
    output = workdir / "reference.mp4"
    ffmpeg_reference(tracks, output)
    return {"playlists": track_playlists, "size": output.stat().st_size, "sha256": sha256(output)}


def evaluate_json(client: cdp_drive.CDP, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def wait_mux_player(client: cdp_drive.CDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = evaluate_json(
                client,
                "(()=>{const m=document.querySelector('mux-video');if(!m)return {mux:false};"
                "let media=null;const walk=root=>{if(media)return;root.querySelectorAll('*').forEach(el=>{if(media)return;if(el.shadowRoot){const v=el.shadowRoot.querySelector('video');if(v){media=v;return;}walk(el.shadowRoot);}});};walk(document);"
                "const r=media?.getBoundingClientRect();"
                "return {mux:true,muxRect:(()=>{const x=m.getBoundingClientRect();return {w:x.width,h:x.height}})()};"
                "})()",
            )
            if not last.get("mux"):
                time.sleep(0.5)
                continue
            player = evaluate_json(
                client,
                "(()=>{let media=null;const walk=root=>{if(media)return;root.querySelectorAll('*').forEach(el=>{if(media)return;if(el.shadowRoot){const v=el.shadowRoot.querySelector('video');if(v){media=v;return;}walk(el.shadowRoot);}});};walk(document);"
                "if(!media)return {found:false};const r=media.getBoundingClientRect();"
                "return {found:true,src:media.currentSrc||media.src||'',readyState:media.readyState,paused:media.paused,duration:media.duration,"
                "rect:{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height},"
                "lightMedia:document.querySelectorAll('video,audio').length,"
                "button:!!document.querySelector('#dm-media-download-button')};})()",
            )
            last = player
            if player.get("found") and player.get("readyState", 0) >= 2 and not player.get("paused") and player.get("button"):
                return player
        except Exception as error:
            last = {"error": repr(error)}
        time.sleep(0.5)
    raise RuntimeError(f"mux-video player did not become playable/injected: {last}")


def play_control(client: cdp_drive.CDP) -> dict:
    return evaluate_json(
        client,
        "(()=>{const candidates=[...document.querySelectorAll('media-play-button,button[aria-label=\"Play\"],button[aria-label=\"play\"]')];"
        "const target=candidates.find(item=>{const r=item.getBoundingClientRect();return r.width>0&&r.height>0;})||candidates[0];"
        "if(!target)return {error:'no play control'};const r=target.getBoundingClientRect();"
        "return {selector:target.tagName.toLowerCase(),aria:target.getAttribute('aria-label'),title:target.getAttribute('title'),"
        "x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height,top:r.top,left:r.left};})()",
    )


def click(client: cdp_drive.CDP, point: dict) -> None:
    for event, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call("Input.dispatchMouseEvent", {"type": event, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1, "modifiers": 0})


def button_point(client: cdp_drive.CDP) -> dict:
    return evaluate_json(
        client,
        "(()=>{const b=document.querySelector('#dm-media-download-button');if(!b)throw Error('media button missing');const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height};})()",
    )


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-muxvideo-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [
                str(public.CHROME),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}",
                f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}",
                f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,1000",
                "--autoplay-policy=no-user-gesture-required",
                "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        deadline = time.time() + 60
        loaded = None
        while time.time() < deadline:
            try:
                loaded = evaluate_json(client, "{href:location.href,state:document.readyState}")
                if loaded.get("href", "").startswith(PAGE) and loaded.get("state") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        control = play_control(client)
        if control.get("error"):
            raise RuntimeError(control["error"])
        print("MUXVIDEO-PLAY-CONTROL:", json.dumps(control, sort_keys=True), flush=True)
        click(client, control)
        player = wait_mux_player(client)
        print("MUXVIDEO-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        button = button_point(client)
        print("MUXVIDEO-BUTTON:", json.dumps(button, sort_keys=True), flush=True)
        click(client, button)
        db = public.db_path(str(home))
        deadline = time.time() + 90
        job = None
        while time.time() < deadline:
            candidates = [item for item in public.jobs(db) if item.get("media") is True]
            if candidates:
                job = candidates[-1]
                break
            time.sleep(0.5)
        if not job:
            raise RuntimeError("no native media job created")
        if "mux.com" not in job["source"]:
            raise RuntimeError(f"native job source is not the Mux stream: {job}")
        import re as _re
        if _re.search(r"(^|[/_-])(subtitles?|captions?)([/_.-]|$)", job["source"], flags=_re.I):
            raise RuntimeError(f"native job captured a subtitle playlist instead of media: {job}")
        print("MUXVIDEO-NATIVE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "media": job.get("media"), "state": job.get("state"), "name": job.get("name")}, sort_keys=True), flush=True)
        destination = root / "Downloads" / "mux-video.mp4"
        public.commit_via_cli(str(home), inspector_port, job["id"], "mux-video.mp4", str(destination))
        reference_dir = root / "reference"
        reference_dir.mkdir(parents=True, exist_ok=True)
        reference = build_reference(job["source"], reference_dir)
        print("MUXVIDEO-REFERENCE:", json.dumps({"playlists": reference["playlists"], "size": reference["size"], "sha256": reference["sha256"]}, sort_keys=True), flush=True)
        completed = public.wait_completed(db, job["id"], timeout=360)
        output_size = destination.stat().st_size
        output_hash = sha256(destination)
        assert output_size == reference["size"], (completed, output_size, reference)
        assert output_hash == reference["sha256"], (completed, output_hash, reference)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(
            f"MUXVIDEO-CHROMIUM: PASS (page={PAGE}, source={job['source']}, "
            f"output_bytes={output_size}, sha256={output_hash}, "
            f"jobs={len(public.jobs(db))}, browser_downloads={browser_files}, "
            f"shadow_media=true, light_media={player['lightMedia']})",
            flush=True,
        )
        print("MUXVIDEO-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
        raise
    finally:
        if client is not None:
            try:
                client.sock.close()
            except Exception:
                pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=10)
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_MUXVIDEO") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MUXVIDEO-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
