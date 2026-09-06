#!/usr/bin/env python3
"""Real public player: Dailymotion geo player embed -> DM button -> capture.

The homepage hero is a Video.js player feeding a <video> via blob MSE
(VHS). The probe starts playback with a trusted play click, clicks the REAL
extension media button, and commits through the real provisional-to-completed
flow. Reference = an independent fetch (Chrome UA) of the exact source URL
the job captured. Asserts byte/hash equality, exactly one job, empty
Chromium Downloads.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://www.dailymotion.com/embed/video/x7svh5p"
CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/149.0.0.0 Safari/537.36"

PLAY_JS = (
    "JSON.stringify((()=>{const b=document.querySelector('video')||document.querySelector('video');"
    "const r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
)
BUTTON_JS = (
    "JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');"
    "if(!b)return null;const r=b.getBoundingClientRect();"
    "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
)
STATE_JS = (
    "JSON.stringify((()=>{const v=document.querySelector('video');"
    "return {t:v?v.currentTime:-1,src:((v&&(v.currentSrc||v.src))||'').slice(0,120)};})())"
)


def click(client, point) -> None:
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})


def wait_media_job(db, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: list = []
    while time.time() < deadline:
        try:
            jobs = public.jobs(db)
            media = [job for job in jobs if job.get("media")]
            if media:
                return media[-1]
            last = [job.get("id") for job in jobs]
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"no media job appeared; jobs seen: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-dailymotion-button-"))
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
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    app_log.close()
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
             f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
             f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
             "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        time.sleep(8.0)
        # Click video center only if playback has not started on its own
        # (a blind click would pause an already-playing embed).
        try:
            prestate = json.loads(client.evaluate(STATE_JS))
        except Exception:
            prestate = {"t": 0}
        if not prestate or prestate.get("t", 0) <= 0:
            click(client, json.loads(client.evaluate(PLAY_JS)))
            print("DAILYMOTION-PLAY-CLICK: issued", flush=True)
        else:
            print(f"DAILYMOTION-PLAY-CLICK: skipped (already t={prestate.get('t')})", flush=True)
        deadline = time.time() + 90
        point = None
        state = {}
        while time.time() < deadline:
            try:
                raw = client.evaluate(BUTTON_JS)
                state = json.loads(client.evaluate(STATE_JS))
                if raw and json.loads(raw):
                    point = json.loads(raw)
                    break
            except Exception as error:
                state = {"error": str(error)[:120]}
            time.sleep(1.0)
        print("DAILYMOTION-STATE:", json.dumps(state, sort_keys=True), flush=True)
        if point is None:
            raise RuntimeError(f"media button never appeared: {state}")
        click(client, point)
        db = public.db_path(str(home))
        created = wait_media_job(db)
        source = created.get("source")
        print("DAILYMOTION-JOB:", json.dumps({"id": created["id"], "media": created.get("media"), "kind": created.get("kind"), "source": source}, sort_keys=True), flush=True)
        assert created.get("media") is True and source and not source.startswith("blob:"), created
        print("DAILYMOTION-REFERENCE-FETCH: resolving manifest + muxing reference...", flush=True)
        manifest_request = urllib.request.Request(source, headers={"User-Agent": CHROME_UA, "Referer": PAGE})
        with urllib.request.urlopen(manifest_request, timeout=120) as response:
            manifest = response.read().decode("utf-8", "replace")

        def fetch_fragment(url: str) -> bytes:
            fragment_request = urllib.request.Request(url, headers={"User-Agent": CHROME_UA, "Referer": PAGE})
            with urllib.request.urlopen(fragment_request, timeout=180) as fragment_response:
                return fragment_response.read()

        track_urls: list[str] = []
        # RFC 8216 4.4.2.4: EXT-X-KEY applies to subsequent segments; without
        # an explicit IV the media sequence number is the 128-bit BE IV.
        base_sequence = 0
        segment_index = 0
        pending_key: dict | None = None
        track_keys: list = []
        for line in manifest.splitlines():
            text = line.strip()
            if text.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    base_sequence = int(text.split(":", 1)[1].strip())
                except ValueError:
                    raise RuntimeError("reference: invalid media sequence number")
                continue
            if text.startswith("#EXT-X-KEY:"):
                attrs = text.split(":", 1)[1]
                method = ""
                uri = None
                iv_hex = None
                for part in attrs.split(","):
                    if "=" not in part:
                        continue
                    k, _, v = part.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"')
                    if k == "METHOD":
                        method = v
                    elif k == "URI":
                        uri = v
                    elif k == "IV":
                        iv_hex = v
                if method == "NONE":
                    pending_key = None
                    continue
                if method != "AES-128" or not uri:
                    raise RuntimeError(f"reference: unsupported encryption method: {method}")
                key_url = urllib.parse.urljoin(source, uri)
                if iv_hex is not None:
                    iv_clean = iv_hex[2:] if iv_hex.lower().startswith("0x") else iv_hex
                    if len(iv_clean) != 32:
                        raise RuntimeError("reference: invalid key IV")
                    iv_bytes = bytes.fromhex(iv_clean)
                else:
                    iv_bytes = None
                pending_key = {"uri": key_url, "iv": iv_bytes}
                continue
            if text.startswith("#EXT-X-MAP:"):
                uri = text.split('URI="', 1)[1].split('"', 1)[0]
                track_urls.insert(0, urllib.parse.urljoin(source, uri))
                track_keys.insert(0, None)
            elif text and not text.startswith("#"):
                track_urls.append(urllib.parse.urljoin(source, text))
                if pending_key is None:
                    track_keys.append(None)
                else:
                    sequence = base_sequence + segment_index
                    iv = pending_key["iv"] if pending_key["iv"] is not None else sequence.to_bytes(16, "big")
                    track_keys.append({"uri": pending_key["uri"], "iv": iv})
                segment_index += 1
        assert track_urls, "manifest resolved to no fragments"

        def decrypt_aes128(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
            proc = subprocess.run(
                ["openssl", "enc", "-d", "-aes-128-cbc",
                 "-K", key.hex(), "-iv", iv.hex()],
                input=ciphertext, capture_output=True, check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"reference key decrypt failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
            return proc.stdout

        key_cache: dict[str, bytes] = {}

        def fetch_key(url: str) -> bytes:
            if url not in key_cache:
                key_request = urllib.request.Request(url, headers={"User-Agent": CHROME_UA, "Referer": PAGE})
                with urllib.request.urlopen(key_request, timeout=60) as key_response:
                    data = key_response.read()
                if len(data) != 16:
                    raise RuntimeError(f"reference: key did not contain 16 bytes (got {len(data)})")
                key_cache[url] = data
            return key_cache[url]

        track_path = root / "dailymotion-reference-video.track"
        with track_path.open("wb") as handle:
            for fragment_url, fragment_key in zip(track_urls, track_keys):
                raw = fetch_fragment(fragment_url)
                if fragment_key is None:
                    handle.write(raw)
                else:
                    handle.write(decrypt_aes128(raw, fetch_key(fragment_key["uri"]), fragment_key["iv"]))
        reference_path = root / "dailymotion-reference.mp4"
        # Same mux path as the resident finalize_media (-map 0 -c copy):
        # comparing against a narrower map would drop streams the app keeps.
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(track_path), "-map", "0", "-c", "copy", str(reference_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"reference ffmpeg failed: {result.stderr.strip()}")
        reference = reference_path.read_bytes()
        reference_hash = hashlib.sha256(reference).hexdigest()
        print(f"DAILYMOTION-REFERENCE: fragments={len(track_urls)} bytes={len(reference)} sha256={reference_hash}", flush=True)
        managed = managed_dir / "dailymotion-capture.mp4"
        public.commit_via_cli(str(home), inspector_port, created["id"], "dailymotion-capture.mp4", str(managed))
        done = public.wait_completed(db, created["id"], timeout=600)
        assert done.get("provisional") is False, done
        output = managed.read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        assert len(output) == len(reference) and output_hash == reference_hash, (len(output), output_hash)
        all_jobs = public.jobs(db)
        assert len(all_jobs) == 1, [job.get("id") for job in all_jobs]
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"DAILYMOTION: PASS (output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("DAILYMOTION-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}", flush=True)
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
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"DAILYMOTION-PROBE: FAIL: {error}", flush=True)
        raise
