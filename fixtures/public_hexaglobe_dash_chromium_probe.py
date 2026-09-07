#!/usr/bin/env python3
"""Real Chromium + Hexaglobe Player DASH media proof.

The official Hexaglobe Player demo's fully featured VOD page drives a real
Shaka/MSE-backed HTML video element with a finite public Big Buck Bunny MPD.
The probe uses a fresh Chromium profile, the real unpacked extension/native
host, browser CDP traffic, a resident real binary, and a product
player-bound Download click. It requires a completed, non-provisional native
container with video/audio streams and no browser file.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_dash_quality_probe as adaptive
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
DASH_SOURCE = "https://dash.akamaized.net/akamai/bbb_30fps/bbb_with_tiled_thumbnails.mpd"
VIDEO_PAGE = "https://player-demo.hexaglobe.net/vod-fully-featured.html"
PAGE = VIDEO_PAGE


def redacted_url(url: str) -> str:
    """Keep host/path and query keys, never signed query values."""
    parts = urlsplit(url)
    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    query = "&".join(f"{key}=[REDACTED]" for key in keys)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def browser_dash_reference(root: Path, selected_segments: list[str]) -> dict:
    manifest_bytes = fetch_bytes(DASH_SOURCE)
    namespace = "{urn:mpeg:dash:schema:mpd:2011}"
    manifest = ET.fromstring(manifest_bytes)
    duration = float((manifest.get("mediaPresentationDuration") or "PT0S").removeprefix("PT").removesuffix("S"))
    base = urljoin(DASH_SOURCE, manifest.findtext(f"{namespace}BaseURL") or "./")
    tracks = []
    for adaptation in manifest.findall(f".//{namespace}AdaptationSet"):
        kind = (adaptation.get("contentType") or adaptation.get("mimeType", "")).split("/", 1)[0].lower()
        if kind not in {"video", "audio"}:
            continue
        template = adaptation.find(f"{namespace}SegmentTemplate")
        if template is None:
            raise RuntimeError(f"Hexaglobe MPD has no SegmentTemplate for {kind}")
        representations = adaptation.findall(f"{namespace}Representation")
        if not representations:
            raise RuntimeError(f"Hexaglobe MPD has no {kind} Representation")
        chosen = next(
            (representation for representation in representations if any(
                f"/{representation.get('id', '')}/" in urlsplit(selected).path
                for selected in selected_segments
            )),
            None,
        )
        if chosen is None:
            raise RuntimeError(f"browser-selected {kind} representation was not present in native hints: {selected_segments}")
        representation_id = chosen.get("id", "")
        bandwidth = chosen.get("bandwidth", "")
        timescale = int(template.get("timescale", "1"))
        segment_duration = int(template.get("duration", "0"))
        if not segment_duration:
            raise RuntimeError(f"Hexaglobe MPD has no finite {kind} segment duration")
        start_number = int(template.get("startNumber", "1"))
        count = math.ceil(duration * timescale / segment_duration)
        def expand(value: str, number: int) -> str:
            return value.replace("$RepresentationID$", representation_id).replace("$Bandwidth$", bandwidth).replace("$Number$", str(number))
        urls = [urljoin(base, expand(template.get("initialization", ""), 0))]
        urls.extend(urljoin(base, expand(template.get("media", ""), start_number + index)) for index in range(count))
        output = root / f"hexa-reference-{kind}.m4{'v' if kind == 'video' else 'a'}"
        with output.open("wb") as stream:
            for url in urls:
                stream.write(fetch_bytes(url))
        tracks.append({"kind": kind, "representation": representation_id, "segments": len(urls), "url": urljoin(base, expand(template.get("media", ""), start_number)), "bytes": output.stat().st_size, "sha256": sha256(output), "path": output})
    if {track["kind"] for track in tracks} != {"video", "audio"}:
        raise RuntimeError(f"Hexaglobe reference did not resolve audio/video tracks: {tracks}")
    video = next(track for track in tracks if track["kind"] == "video")
    audio = next(track for track in tracks if track["kind"] == "audio")
    output = root / "hexa-reference-mux.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video["path"]), "-i", str(audio["path"]), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(output)], check=True)
    return {"manifest": DASH_SOURCE, "duration": duration, "tracks": [{key: value for key, value in track.items() if key != "path"} for track in tracks], "output_bytes": output.stat().st_size, "output_sha256": sha256(output)}


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_media_job(db: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if job.get("media") is True]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"Hexaglobe Player capture created no native media job: {last}")


def wait_player(client: adaptive.EventCDP, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    last_raw = None
    last_error = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=Array.from(document.querySelectorAll('audio,video'));"
                "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
                "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
                "const v=media.find(item=>sourceOf(item)&&visible(item))||media.find(item=>sourceOf(item))||media[0];"
                "if(v){v.muted=true;void v.play();}"
                "const r=v?.getBoundingClientRect();"
                "return {title:document.title,videoCount:media.length,videos:media.map(item=>({src:sourceOf(item),readyState:item.readyState,paused:item.paused,ended:item.ended,rect:(()=>{const box=item.getBoundingClientRect();return {top:box.top,left:box.left,right:box.right,bottom:box.bottom,width:box.width,height:box.height}})()})),"
                "src:v?sourceOf(v):'',readyState:v?.readyState||0,paused:v?.paused??true,"
                "ended:v?.ended??false,duration:v?.duration??0,error:v?.error?.message||null,"
                "button:!!document.querySelector('#dm-media-download-button'),"
                "rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null,"
                "body:document.body?.innerText?.slice(0,400)||''};})())"
            )
            last_raw = repr(raw)
            last = json.loads(raw)
            if last.get("videoCount", 0) and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button"):
                return last
        except Exception as error:
            last_error = repr(error)
        time.sleep(0.5)
    diagnostic = None
    try:
        diagnostic = client.evaluate("JSON.stringify({href:location.href,title:document.title,readyState:document.readyState,body:document.body?.innerText?.slice(0,800)||'',videos:document.querySelectorAll('audio,video').length,videoHtml:Array.from(document.querySelectorAll('audio,video')).map(v=>v.outerHTML.slice(0,1200)),sources:Array.from(document.querySelectorAll('audio source,video source')).map(s=>({attr:s.getAttribute('src'),prop:s.src,type:s.getAttribute('type')})),iframes:Array.from(document.querySelectorAll('iframe')).map(frame=>frame.src).slice(0,8)})")
    except Exception as error:
        diagnostic = repr(error)
    raise RuntimeError(f"Hexaglobe Player did not become playable/injected: last={last}; raw={last_raw}; error={last_error}; diagnostic={diagnostic}")


def activate_player(client: adaptive.EventCDP) -> None:
    deadline = time.time() + 30
    point = None
    raw = 'null'
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const media=Array.from(document.querySelectorAll('video'));"
            "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
            "const visible=item=>{const box=item.getBoundingClientRect();return box.width>=120&&box.height>=40&&box.bottom>0&&box.right>0&&box.top<innerHeight&&box.left<innerWidth};"
            "const v=media.find(item=>sourceOf(item)&&visible(item))||media.find(item=>sourceOf(item));"
            "if(!v)return null;v.scrollIntoView({block:'center'});const r=v.getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2,source:sourceOf(v),readyState:v.readyState,paused:v.paused};})())"
        )
        point = json.loads(raw)
        if point:
            break
        time.sleep(0.5)
    if not point:
        raise RuntimeError(f"Hexaglobe Player surface missing: {raw}")
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    deadline = time.time() + 25
    while time.time() < deadline:
        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const media=Array.from(document.querySelectorAll('video'));"
            "const sourceOf=item=>item.currentSrc||item.src||item.querySelector('source[src]')?.src||item.querySelector('source[src]')?.getAttribute('src')||'';"
            "const v=media.find(item=>sourceOf(item))||media[0];"
            "const source=v?sourceOf(v):'';"
            "if(v&&source){v.muted=true;void v.play();}"
            "return {source,disabled:v?.hasAttribute('disabled')??true,readyState:v?.readyState||0,paused:v?.paused??true};})())"
        )
        state = json.loads(raw)
        if state.get("source") and not state.get("disabled"):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Hexaglobe Player did not instantiate after surface click: {raw}")


def collect_media_events(client: adaptive.EventCDP) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for event in client.take_events():
        if event.get("method") != "Network.requestWillBeSent":
            continue
        params = event.get("params", {})
        request = params.get("request", {})
        url = request.get("url", "")
        host = urlsplit(url).netloc.lower()
        if host != "dash.akamaized.net":
            continue
        if not urlsplit(url).path.lower().endswith((".mpd", ".m4v", ".m4a")):
            continue
        key = (url, str(params.get("requestId")))
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "url": redacted_url(url),
            "host": host,
            "type": request.get("method"),
            "range": next((value for key, value in request.get("headers", {}).items() if str(key).lower() == "range"), None),
            "requestId": params.get("requestId"),
        })
    return records


def wait_for_traffic(client: adaptive.EventCDP, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    records: list[dict] = []
    while time.time() < deadline:
        records.extend(collect_media_events(client))
        if any(item["url"].lower().split("?")[0].endswith(".mpd") for item in records) and any(item["url"].lower().split("?")[0].endswith((".m4v", ".m4a")) for item in records):
            return records
        time.sleep(0.5)
    return records


def button_point(client: adaptive.EventCDP) -> dict:
    return json.loads(client.evaluate(
        "JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');"
        "if(!b)throw Error('Download button missing');const r=b.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,text:b.textContent};})())"
    ))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-hexaglobe-dash-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
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
                str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
        client = adaptive.connect_event_chrome(chrome_port)
        public.wait_page(client, PAGE)
        activate_player(client)
        player = wait_player(client)
        print("HEXA-DASH-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        initial_traffic = wait_for_traffic(client)
        print("HEXA-DASH-TRAFFIC:", json.dumps(initial_traffic[:12], sort_keys=True), flush=True)
        point = button_point(client)
        print("HEXA-DASH-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_media_job(db)
        media_name = "hexa-dash-bbb.mp4"
        managed = managed_dir / media_name
        selected_segments = job.get("selected_segments", []) or job.get("selectedSegments", [])
        print("HEXA-DASH-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "name": job.get("name"), "selected_segments": [redacted_url(url) for url in selected_segments]}, sort_keys=True), flush=True)
        assert job.get("source") == DASH_SOURCE and selected_segments, job
        traffic = initial_traffic + collect_media_events(client)
        print("HEXA-DASH-SELECTED-TRAFFIC:", json.dumps(traffic[:24], sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=240)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        reference = browser_dash_reference(root, selected_segments)
        print("HEXA-DASH-REFERENCE:", json.dumps(reference, sort_keys=True), flush=True)
        assert reference["manifest"] == DASH_SOURCE and {track["kind"] for track in reference["tracks"]} == {"video", "audio"}, reference
        if native_size != reference["output_bytes"] or native_hash != reference["output_sha256"]:
            raise RuntimeError(f"native output differs from independent Hexaglobe DASH reference: native={native_size}/{native_hash} reference={reference['output_bytes']}/{reference['output_sha256']}")
        ffprobe_run = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(managed)],
            check=True, capture_output=True, text=True,
        )
        ffprobe = json.loads(ffprobe_run.stdout)
        streams = ffprobe.get("streams", [])
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert video_streams and audio_streams, ffprobe
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        print("HEXA-DASH-NATIVE-RESULT:", json.dumps({"state": done["state"], "provisional": done.get("provisional"), "bytes": native_size, "sha256": native_hash, "ffprobe": ffprobe, "reference": reference, "browser_downloads": browser_files}, sort_keys=True), flush=True)
        print(f"HEXA-DASH-CHROMIUM: PASS (page={PAGE}, source={redacted_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, traffic={len(traffic)}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("HEXA-DASH-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
        raise
    finally:
        if client is not None:
            try: client.sock.close()
            except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill(); chrome.wait(timeout=10)
        if app.poll() is None:
            app.terminate()
            try: app.wait(timeout=10)
            except subprocess.TimeoutExpired: app.kill(); app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_HEXA_DASH") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"HEXA-DASH-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
