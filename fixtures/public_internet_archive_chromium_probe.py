#!/usr/bin/env python3
"""Real Chromium/native proof: Internet Archive public-domain item player.

The Internet Archive item page uses a nested open-shadow player:
``ia-music-theater -> play-av -> JW controls -> <video>``. This probe selects
track 2 through the real track-list button, starts it through the real player
control, clicks the extension's injected player-bound Download button, commits
the managed job through the resident CLI, and compares output bytes/hash with an
independent browser-context fetch of the exact selected source.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

PAGE = "https://archive.org/details/HumpbackWhalesSongsSoundsVocalizations"
TRACK_NUMBER = "1"
TRACK_TOKEN = "Humpback_whale_song_3"
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME


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
          const roots=[];
          const seen=new Set();
          const walk=(root)=>{
            if(!root||seen.has(root))return;
            seen.add(root);
            if(!root.querySelectorAll)return;
            for(const el of root.querySelectorAll('*')){
              if(el.shadowRoot)walk(el.shadowRoot);
            }
          };
          walk(document);
          const all=(selector)=>{
            const result=[];
            for(const root of seen){
              for(const el of root.querySelectorAll?.(selector)||[])result.push(el);
            }
            return result;
          };
          const box=(el)=>{
            const r=el?.getBoundingClientRect();
            return r?{x:r.x,y:r.y,w:r.width,h:r.height}:null;
          };
          const sourceOf=(el)=>el?.currentSrc||el?.src||el?.querySelector('source[src]')?.src||el?.querySelector('source[src]')?.getAttribute('src')||'';
          const media=all('video,audio');
          const video=media.find(el=>el.tagName==='VIDEO')||media[0]||null;
          const tracks=all('button.track');
          const player=all('[role="button"][aria-label="Play"],[role="button"][aria-label="Pause"]').find(el=>box(el)?.w>0&&box(el)?.h>0)||null;
          const download=document.querySelector('#dm-media-download-button');
          return {
            href:location.href,
            ready:document.readyState,
            roots:roots.length,
            media:media.map(el=>({tag:el.tagName,src:sourceOf(el),readyState:el.readyState,paused:el.paused,ended:el.ended,duration:el.duration,rect:box(el)})),
            source:sourceOf(video),
            readyState:video?.readyState||0,
            paused:video?.paused??true,
            ended:video?.ended??false,
            duration:video?.duration??0,
            videoRect:box(video),
            selectedTrack:tracks.find(el=>el.classList.contains('selected'))?.dataset.trackNumber??null,
            tracks:tracks.map(el=>({number:el.dataset.trackNumber,text:el.textContent.trim(),selected:el.classList.contains('selected'),rect:box(el)})),
            player:player?{aria:player.getAttribute('aria-label'),rect:box(player)}:null,
            download:download?{rect:box(download),text:download.textContent.trim()}:null
          };
        })()""",
    )


def wait_page(client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = deep_state(client)
            if (
                last.get("ready") == "complete"
                and last.get("href", "").startswith(PAGE)
                and len(last.get("tracks", [])) >= 2
                and last.get("media")
            ):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Internet Archive page/player did not become ready: {last}")


def point_for(client, expression: str) -> dict:
    return evaluate_json(client, expression)


def track_point(client) -> dict:
    return point_for(
        client,
        f"""(()=>{{
          const roots=[];const seen=new Set();
          const walk=(root)=>{{if(!root||seen.has(root))return;seen.add(root);for(const el of root.querySelectorAll?.('*')||[])if(el.shadowRoot)walk(el.shadowRoot);}};
          walk(document);
          for(const root of seen){{
            const el=root.querySelector?.('button.track[data-track-number={json.dumps(TRACK_NUMBER)}]');
            if(el){{el.scrollIntoView({{block:'center'}});const r=el.getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2,text:el.textContent.trim(),number:el.dataset.trackNumber}};}}
          }}
          return {{error:'track button missing'}};
        }})()""",
    )


def play_point(client) -> dict:
    return point_for(
        client,
        """(()=>{
          const roots=[];const seen=new Set();
          const walk=(root)=>{if(!root||seen.has(root))return;seen.add(root);for(const el of root.querySelectorAll?.('*')||[])if(el.shadowRoot)walk(el.shadowRoot);};
          walk(document);
          for(const root of seen){
            const el=root.querySelector?.('[role="button"][aria-label="Play"]');
            if(el){el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,aria:el.getAttribute('aria-label')};}
          }
          return {error:'play control missing'};
        })()""",
    )


def wait_selected_playing(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = deep_state(client)
        if (
            last.get("selectedTrack") == TRACK_NUMBER
            and TRACK_TOKEN in last.get("source", "")
            and last.get("readyState", 0) >= 2
            and not last.get("paused")
            and not last.get("ended")
        ):
            return last
        if (
            last.get("selectedTrack") == TRACK_NUMBER
            and TRACK_TOKEN in last.get("source", "")
            and last.get("paused")
        ):
            point = play_point(client)
            if not point.get("error"):
                click(client, point)
        time.sleep(0.5)
    raise RuntimeError(f"Internet Archive track did not become playing: {last}")


def wait_download_button(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = deep_state(client)
        if last.get("download", {}).get("rect"):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"extension media button did not appear: {last}")


def browser_reference(client, url: str) -> dict:
    raw = client.evaluate(
        f"""(async()=>{{
          const response=await fetch({json.dumps(url)},{{cache:'no-store'}});
          if(!response.ok)return JSON.stringify({{error:'HTTP '+response.status}});
          const bytes=new Uint8Array(await response.arrayBuffer());
          const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));
          return JSON.stringify({{size:bytes.length,sha256:Array.from(digest).map(value=>value.toString(16).padStart(2,'0')).join('')}});
        }})()"""
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser reference failed for selected source: {result}")
    return result


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-internet-archive-"))
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
        print("INTERNET-ARCHIVE-PAGE:", json.dumps(page, sort_keys=True), flush=True)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)

        selected_point = track_point(client)
        if selected_point.get("error"):
            raise RuntimeError(selected_point["error"])
        print("INTERNET-ARCHIVE-TRACK-CLICK:", json.dumps(selected_point, sort_keys=True), flush=True)
        click(client, selected_point)
        selected = deep_state(client)
        print("INTERNET-ARCHIVE-SELECTED:", json.dumps(selected, sort_keys=True), flush=True)
        playing = wait_selected_playing(client)
        print("INTERNET-ARCHIVE-PLAYING:", json.dumps(playing, sort_keys=True), flush=True)
        button_state = wait_download_button(client)
        button = button_state["download"]["rect"]
        print("INTERNET-ARCHIVE-DOWNLOAD-BUTTON:", json.dumps(button_state["download"], sort_keys=True), flush=True)
        click(client, {"x": button["x"] + button["w"] / 2, "y": button["y"] + button["h"] / 2})

        db = public.db_path(str(home))
        deadline = time.time() + 120
        job = None
        while time.time() < deadline:
            candidates = [
                item for item in public.jobs(db)
                if item.get("media") is True and TRACK_TOKEN in item.get("source", "")
            ]
            if candidates:
                job = candidates[-1]
                break
            time.sleep(0.5)
        if not job:
            raise RuntimeError(f"no selected Internet Archive media job: {public.jobs(db)}")
        print(
            "INTERNET-ARCHIVE-NATIVE-JOB:",
            json.dumps(
                {
                    "id": job["id"],
                    "source": job["source"],
                    "media": job.get("media"),
                    "state": job.get("state"),
                    "provisional": job.get("provisional"),
                    "name": job.get("name"),
                },
                sort_keys=True,
            ),
            flush=True,
        )

        media_name = "internet-archive-humpback-song-3.mp3"
        managed = managed_dir / media_name
        public.commit_via_cli(str(home), inspector_port, job["id"], media_name, str(managed))
        reference = browser_reference(client, job["source"])
        print("INTERNET-ARCHIVE-REFERENCE:", json.dumps({"url": job["source"], **reference}, sort_keys=True), flush=True)
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
            f"INTERNET-ARCHIVE: PASS (page={PAGE}, source={job['source']}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})",
            flush=True,
        )
        print("INTERNET-ARCHIVE-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_INTERNET_ARCHIVE") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"INTERNET-ARCHIVE-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
