#!/usr/bin/env python3
"""Real Chromium/native proof for the official Media Chrome homepage player."""
from __future__ import annotations

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

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://www.media-chrome.org/"


def wait_player(client, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify((()=>{"
                "const media=[...document.querySelectorAll('video,audio')];"
                "const visible=item=>{const r=item.getBoundingClientRect();return r.width>=120&&r.height>=40&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth};"
                "const item=media.find(visible)||media.find(candidate=>candidate.currentSrc||candidate.src);"
                "if(item){item.muted=true;void item.play();}"
                "const r=item?.getBoundingClientRect();const b=document.querySelector('#dm-media-download-button');const br=b?.getBoundingClientRect();"
                "return {source:item?.currentSrc||item?.src||'',readyState:item?.readyState||0,paused:item?.paused??true,duration:item?.duration??0,media:media.length,button:!!b,buttonRect:br?{top:br.top,left:br.left,right:br.right,bottom:br.bottom,width:br.width,height:br.height}:null,rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null};})())"
            )
            last = json.loads(raw)
            if last.get("source") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button") and last.get("buttonRect"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Media Chrome player did not become playable/injected: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-media-chrome-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    destination = root / "Downloads" / "media-chrome-homepage.mp4"
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
                "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, PAGE)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        player = wait_player(client)
        print("MEDIA-CHROME-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        point = {"x": player["buttonRect"]["left"] + player["buttonRect"]["width"] / 2, "y": player["buttonRect"]["top"] + player["buttonRect"]["height"] / 2}
        print("MEDIA-CHROME-BUTTON:", json.dumps({**point, "trustedInput": True}, sort_keys=True), flush=True)
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = public.wait_job(db, player["source"], timeout=90)
        assert job.get("media") is True and job.get("source") == player["source"], job
        print("MEDIA-CHROME-NATIVE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "media": job.get("media"), "state": job.get("state"), "name": job.get("name")}, sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], destination.name, str(destination))
        reference_size, reference_hash = public.sha256_browser(client, job["source"])
        print("BROWSER-MEDIA-CHROME-REFERENCE:", json.dumps({"url": job["source"], "size": reference_size, "hash": reference_hash}, sort_keys=True), flush=True)
        completed = public.wait_completed(db, job["id"], timeout=300)
        output_size = destination.stat().st_size
        output_hash = support.sha256(str(destination))
        assert output_size == reference_size, (completed, output_size, reference_size)
        assert output_hash == reference_hash, (completed, output_hash, reference_hash)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print("MEDIA-CHROME-NATIVE-RESULT:", json.dumps({"state": completed["state"], "provisional": completed.get("provisional"), "bytes": output_size, "sha256": output_hash, "browser_downloads": browser_files}, sort_keys=True), flush=True)
        print(f"PUBLIC-MEDIA-CHROME-CHROMIUM: PASS (page={PAGE}, source={job['source']}, output_bytes={output_size}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("PUBLIC-MEDIA-CHROME-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_MEDIA_CHROME") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-MEDIA-CHROME-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
