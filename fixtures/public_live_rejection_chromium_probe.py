#!/usr/bin/env python3
"""Real Chromium proof that live HLS is rejected instead of recorded.

The hls.js demo is used as a generic public player. The source is iReplay's
public continuously-generated Blender channel, which has a sliding playlist
window and no EXT-X-ENDLIST. This probe proves the player-bound control is
attached to the currently playing blob/MSE video, then verifies that native
capture rejects the live source cleanly without a commit or browser download.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys_path = Path(__file__).resolve().parent
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(sys_path))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
LIVE_SOURCE = "https://ireplay.tv/test/blender.m3u8"
PAGE = "https://hlsjs.video-dev.org/demo/?src=" + quote(LIVE_SOURCE, safe="")


def source_manifest_shape() -> dict:
    request = urllib.request.Request(LIVE_SOURCE, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(128 * 1024).decode("utf-8", "replace")
        return {
            "status": response.status,
            "content_type": response.headers.get("Content-Type"),
            "has_endlist": any(line.strip().upper() == "#EXT-X-ENDLIST" for line in body.splitlines()),
            "has_media_sequence": any(line.strip().upper().startswith("#EXT-X-MEDIA-SEQUENCE") for line in body.splitlines()),
            "live_variant_count": sum("_live.m3u8" in line for line in body.splitlines()),
        }


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_live_player(client, timeout: float = 150.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "(async()=>{"
                "const v=document.querySelector('video');"
                "if(v){v.scrollIntoView({block:'center'});v.muted=true;try{await v.play()}catch{}}"
                "const vr=v?.getBoundingClientRect();"
                "const b=document.querySelector('#dm-media-download-button');"
                "const br=b?.getBoundingClientRect();"
                "return JSON.stringify({src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,finite:Number.isFinite(v?.duration),button:!!b,videoRect:vr?{top:vr.top,left:vr.left,right:vr.right,bottom:vr.bottom,width:vr.width,height:vr.height}:null,buttonRect:br?{top:br.top,left:br.left,right:br.right,bottom:br.bottom,width:br.width,height:br.height}:null});"
                "})()"
            )
            last = json.loads(raw)
            if (
                last.get("src", "").startswith("blob:")
                and last.get("readyState", 0) >= 2
                and not last.get("paused")
                and last.get("button")
            ):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public live player did not become playable/injected: {last}")


def button_geometry(client) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const v=document.querySelector('video');const b=document.querySelector('#dm-media-download-button');"
        "if(!v||!b)throw Error('live video or Download button missing');"
        "const vr=v.getBoundingClientRect(),br=b.getBoundingClientRect();"
        "const x=br.left+br.width/2,y=br.top+br.height/2;"
        "return {video:{top:vr.top,left:vr.left,right:vr.right,bottom:vr.bottom,width:vr.width,height:vr.height},button:{top:br.top,left:br.left,right:br.right,bottom:br.bottom,width:br.width,height:br.height,x,y},attached:x>=vr.left&&x<=vr.right&&y>=vr.top&&y<=vr.bottom};})())"
    )
    return json.loads(raw)


def wait_live_job(db: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if job.get("media") is True and ".m3u8" in job.get("source", "")]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"live click did not create a media job: {last}")


def wait_failed(db: str, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = next((job for job in jobs(db) if job.get("id") == job_id), None)
        if last and last.get("state") == "failed":
            return last
        time.sleep(0.2)
    raise RuntimeError(f"live media was not rejected promptly: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-live-rejection-chromium-"))
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
    app = subprocess.Popen(
        [BIN],
        env=public.browser_env(str(home), inspector_port),
        stdout=app_log,
        stderr=subprocess.STDOUT,
    )
    chrome = None
    client = None
    try:
        shape = source_manifest_shape()
        assert shape["status"] == 200 and not shape["has_endlist"] and shape["live_variant_count"] >= 1, shape
        print("LIVE-SOURCE:", json.dumps({"source": LIVE_SOURCE, **shape}, sort_keys=True), flush=True)
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [
                str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, PAGE, timeout=120.0)
        player = wait_live_player(client)
        geometry = button_geometry(client)
        assert geometry["attached"], geometry
        print("LIVE-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        print("LIVE-BUTTON:", json.dumps(geometry, sort_keys=True), flush=True)
        point = geometry["button"]
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        created = wait_live_job(db)
        assert created.get("media") is True and created.get("source", "").endswith(".m3u8"), created
        print("LIVE-NATIVE-JOB:", json.dumps({"id": created["id"], "source": created["source"], "media": created.get("media"), "state": created.get("state"), "provisional": created.get("provisional"), "selected_segments": len(created.get("selected_segments", []))}, sort_keys=True), flush=True)
        failed = wait_failed(db, created["id"])
        error = failed.get("error") or ""
        assert "Live media is not supported" in error, failed
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert failed.get("state") == "failed", failed
        assert len(jobs(db)) == 1, jobs(db)
        assert not browser_files, browser_files
        print("LIVE-REJECTION:", json.dumps({"state": failed.get("state"), "error": error, "browser_downloads": browser_files, "jobs": len(jobs(db))}, sort_keys=True), flush=True)
        print(f"PUBLIC-LIVE-REJECTION-CHROMIUM: PASS (page={PAGE}, source={LIVE_SOURCE}, player_src={player['src']}, duration={player['duration']}, output=none, browser_downloads={browser_files}, jobs=1)", flush=True)
        print("PUBLIC-LIVE-REJECTION-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_PUBLIC_LIVE_REJECTION") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-LIVE-REJECTION-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
