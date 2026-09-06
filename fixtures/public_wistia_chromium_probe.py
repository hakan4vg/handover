#!/usr/bin/env python3
"""Real Chromium/native proof: Wistia Aurora custom-player VOD.

The official Wistia demo exposes ``<wistia-player>`` with an open shadow root.
This probe starts the selected public product-video player through its real
shadow-DOM Play control, clicks the injected Download Manager button, commits
one managed job through the resident CLI, and compares exact bytes/hash with an
independent browser-context fetch of the selected public MP4 delivery.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

PAGE = "https://wistia.com/demo?wchannelid=rs59uqxakj&wmediaid=rbsg3da4jd"
MEDIA_ID = "9mz11isa6g"
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "[REDACTED]" if parts.query else "", ""))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_json(client, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def click(client, point: dict) -> None:
    for event, button, buttons in (
        ("mouseMoved", "none", 0),
        ("mousePressed", "left", 1),
        ("mouseReleased", "left", 0),
    ):
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": event,
                "x": point["x"],
                "y": point["y"],
                "button": button,
                "buttons": buttons,
                "clickCount": 1,
                "modifiers": 0,
            },
        )


def deep_state(client) -> dict:
    return evaluate_json(
        client,
        """(()=>{
          const seen=new Set();
          const walk=(root)=>{
            if(!root||seen.has(root))return;
            seen.add(root);
            for(const el of root.querySelectorAll?.('*')||[])if(el.shadowRoot)walk(el.shadowRoot);
          };
          walk(document);
          const roots=[...seen];
          const all=(selector)=>roots.flatMap(root=>[...(root.querySelectorAll?.(selector)||[])]);
          const box=(el)=>{const r=el?.getBoundingClientRect();return r?{x:r.x,y:r.y,w:r.width,h:r.height}:null;};
          const sourceOf=(el)=>el?.currentSrc||el?.src||el?.querySelector('source[src]')?.src||el?.querySelector('source[src]')?.getAttribute('src')||'';
          const media=all('video,audio');
          const video=media.find(el=>el.tagName==='VIDEO')||media[0]||null;
          const controls=all('button,[role="button"]');
          const player=controls.find(el=>{const label=(el.getAttribute('aria-label')||el.getAttribute('title')||el.textContent||'').trim().toLowerCase();const r=box(el);return /^(play|pause|play video|pause video)$/.test(label)&&r?.w>0&&r?.h>0;})||null;
          const download=all('#dm-media-download-button').find(el=>box(el)?.w>0&&box(el)?.h>0)||null;
          return {
            href:location.href,
            ready:document.readyState,
            media:media.map(el=>({tag:el.tagName,src:sourceOf(el),readyState:el.readyState,paused:el.paused,ended:el.ended,currentTime:el.currentTime,duration:el.duration,rect:box(el)})),
            source:sourceOf(video),
            readyState:video?.readyState||0,
            paused:video?.paused??true,
            ended:video?.ended??false,
            currentTime:video?.currentTime??0,
            duration:video?.duration??0,
            videoRect:box(video),
            player:player?{aria:player.getAttribute('aria-label'),title:player.getAttribute('title'),text:player.textContent.trim(),rect:box(player)}:null,
            download:download?{rect:box(download),text:download.textContent.trim()}:null,
            roots:roots.length,
            wistia:[...document.querySelectorAll('wistia-player')].some(el=>el.getAttribute('media-id')==='9mz11isa6g')
          };
        })()""",
    )


def wait_page(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = deep_state(client)
            if last.get("ready") == "complete" and last.get("href", "").startswith(PAGE) and last.get("wistia") and last.get("media"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Wistia page/player did not become ready: {last}")


def point_for(client, expression: str) -> dict:
    return evaluate_json(client, expression)


def play_point(client) -> dict:
    return point_for(
        client,
        """(()=>{
          const seen=new Set();
          const walk=(root)=>{if(!root||seen.has(root))return;seen.add(root);for(const el of root.querySelectorAll?.('*')||[])if(el.shadowRoot)walk(el.shadowRoot);};
          walk(document);
          const roots=[...seen];
          for(const root of roots){
            const candidates=[...root.querySelectorAll?.('button,[role="button"]')||[]];
            for(const el of candidates){
              const label=(el.getAttribute('aria-label')||el.getAttribute('title')||el.textContent||'').trim().toLowerCase();
              const r=el.getBoundingClientRect();
              if(/^(play|play video)$/.test(label)&&r.width>0&&r.height>0){el.scrollIntoView({block:'center'});const q=el.getBoundingClientRect();return {x:q.x+q.width/2,y:q.y+q.height/2,aria:el.getAttribute('aria-label'),title:el.getAttribute('title'),text:el.textContent.trim()};}
            }
          }
          return {error:'Wistia Play control missing'};
        })()""",
    )


def wait_playing(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = deep_state(client)
        if (
            last.get("readyState", 0) >= 2
            and last.get("source", "").startswith(("blob:", "http://", "https://"))
            and last.get("currentTime", 0) > 0
            and not last.get("paused")
            and not last.get("ended")
            and (last.get("videoRect") or {}).get("w", 0) > 0
        ):
            return last
        if last.get("readyState", 0) >= 2 and last.get("paused"):
            point = play_point(client)
            if not point.get("error"):
                click(client, point)
        time.sleep(0.5)
    raise RuntimeError(f"Wistia player did not become playing: {last}")


def wait_download_button(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = deep_state(client)
        if last.get("download", {}).get("rect"):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"extension media button did not appear: {last}")


def hls_fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/149 Safari/537.36", "Referer": PAGE})
    with urlopen(request, timeout=180) as response:
        return response.read()


def hls_reference(source: str, root: Path) -> tuple[Path, dict]:
    manifest_bytes = hls_fetch(source)
    manifest = manifest_bytes.decode("utf-8")
    if "#EXTM3U" not in manifest or "#EXT-X-ENDLIST" not in manifest:
        raise RuntimeError("Wistia source was not a finite HLS VOD playlist")
    selected = source
    lines = [line.strip() for line in manifest.splitlines() if line.strip()]
    variants = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            variants.append(urljoin(source, lines[index + 1]))
    if variants:
        selected = variants[0]
        manifest_bytes = hls_fetch(selected)
        manifest = manifest_bytes.decode("utf-8")
        if "#EXT-X-ENDLIST" not in manifest:
            raise RuntimeError("Wistia selected HLS variant was not finite")
    segment_urls = [urljoin(selected, line) for line in manifest.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not segment_urls:
        raise RuntimeError("Wistia HLS playlist contained no segments")
    if any(line.strip().startswith("#EXT-X-KEY") for line in manifest.splitlines()):
        raise RuntimeError("Wistia HLS playlist unexpectedly requires encryption")

    def fetch_segment(url: str) -> bytes:
        return hls_fetch(url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        bodies = list(pool.map(fetch_segment, segment_urls))
    track = root / "wistia-reference.track"
    with track.open("wb") as stream:
        for body in bodies:
            stream.write(body)
    (root / "wistia-reference.m3u8").write_text(manifest, encoding="utf-8")
    reference = root / "wistia-reference.mp4"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(track), "-map", "0", "-c", "copy", str(reference)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Wistia reference ffmpeg failed: {result.stderr.strip()[-1000:]}")
    return reference, {"manifest_bytes": len(manifest_bytes), "variants": len(variants), "selected": redact_url(selected), "fragments": len(segment_urls), "all_streams": True}


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-wistia-chromium-", dir="/var/tmp"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    managed_dir.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen(
        [BIN],
        env=public.browser_env(str(home), inspector_port),
        stdout=app_log,
        stderr=subprocess.STDOUT,
    )
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [
                str(CHROME),
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
                "--window-size=1280,900",
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
        page = wait_page(client)
        print("WISTIA-PAGE:", json.dumps(page, sort_keys=True), flush=True)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)

        initial = deep_state(client)
        print("WISTIA-INITIAL:", json.dumps(initial, sort_keys=True), flush=True)
        play = play_point(client)
        if play.get("error"):
            raise RuntimeError(play["error"])
        print("WISTIA-PLAY-CLICK:", json.dumps(play, sort_keys=True), flush=True)
        click(client, play)
        playing = wait_playing(client)
        print("WISTIA-PLAYING:", json.dumps(playing, sort_keys=True), flush=True)
        button_state = wait_download_button(client)
        button = button_state["download"]["rect"]
        print("WISTIA-DOWNLOAD-BUTTON:", json.dumps(button_state["download"], sort_keys=True), flush=True)
        click(client, {"x": button["x"] + button["w"] / 2, "y": button["y"] + button["h"] / 2})

        db = public.db_path(str(home))
        deadline = time.time() + 120
        job = None
        while time.time() < deadline:
            candidates = [item for item in public.jobs(db) if item.get("media") is True]
            if candidates:
                job = candidates[-1]
                break
            time.sleep(0.5)
        if not job:
            raise RuntimeError(f"no Wistia media job: {public.jobs(db)}")
        print(
            "WISTIA-NATIVE-JOB:",
            json.dumps(
                {
                    "id": job["id"],
                    "source": redact_url(job["source"]),
                    "media": job.get("media"),
                    "state": job.get("state"),
                    "provisional": job.get("provisional"),
                    "name": job.get("name"),
                },
                sort_keys=True,
            ),
            flush=True,
        )

        media_name = "wistia-player-demo.mp4"
        managed = managed_dir / media_name
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        reference_path, reference_meta = hls_reference(job["source"], root)
        reference = {"size": reference_path.stat().st_size, "sha256": sha256(reference_path), **reference_meta}
        print("WISTIA-REFERENCE:", json.dumps({"url": redact_url(job["source"]), **reference}, sort_keys=True), flush=True)
        completed = public.wait_completed(db, job["id"], timeout=300)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        assert native_size == int(reference["size"]), (completed, native_size, reference)
        assert native_hash == reference["sha256"], (completed, native_hash, reference)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(
            f"WISTIA: PASS (page={PAGE}, source={redact_url(job['source'])}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})",
            flush=True,
        )
        print("WISTIA-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')[-4000:]}", flush=True)
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
        if os.environ.get("DM_KEEP_WISTIA") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"WISTIA-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
