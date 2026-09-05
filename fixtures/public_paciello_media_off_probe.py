#!/usr/bin/env python3
"""Real public Chromium proof that media buttons can be disabled."""
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
import browser_fallback_chromium_probe as fallback
import public_chromium_probe as public
import segmented_restart_probe as support

PAGE = "https://thepaciellogroup.github.io/AT-browser-tests/acc-name-test/audio.html"


def set_media_buttons_off(client) -> dict:
    extension_page = f"chrome-extension://{public.DEV_EXTENSION_ID}/popup.html"
    client.call("Page.navigate", {"url": extension_page})
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            state = json.loads(client.evaluate("JSON.stringify({href:location.href,ready:document.readyState})"))
            if state.get("href") == extension_page and state.get("ready") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError("extension policy page did not load")
    raw = client.evaluate("(async()=>await chrome.runtime.sendMessage({type:'update-policy',patch:{showMediaButtons:false}}))()")
    return raw if isinstance(raw, dict) else json.loads(raw)


def wait_audio(client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate(
                "JSON.stringify((()=>{const v=document.querySelector('audio');"
                "if(v){v.muted=true;void v.play();}"
                "const r=v?.getBoundingClientRect();return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,button:!!document.querySelector('#dm-media-download-button'),rect:r?{width:r.width,height:r.height,top:r.top,left:r.left}:null};})())"
            ))
            if last.get("src") and last.get("readyState", 0) >= 2 and not last.get("paused"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public audio did not become playable: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-paciello-media-off-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    for directory in (home, profile, downloads):
        directory.mkdir(parents=True, exist_ok=True)
    chrome = None
    client = None
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    try:
        chrome = subprocess.Popen(
            [str(fallback.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={fallback.EXTENSION}", f"--disable-extensions-except={fallback.EXTENSION}", "--window-size=1280,900", PAGE],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        policy = set_media_buttons_off(client)
        print("MEDIA-OFF-POLICY:", json.dumps(policy, sort_keys=True), flush=True)
        public.wait_page(client, PAGE)
        play_point = json.loads(client.evaluate("JSON.stringify((()=>{const r=document.querySelector('audio').getBoundingClientRect();return {x:r.left+18,y:r.top+r.height/2}})())"))
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": play_point["x"], "y": play_point["y"], "button": "left", "clickCount": 1})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": play_point["x"], "y": play_point["y"], "button": "left", "clickCount": 1})
        player = wait_audio(client)
        print("PUBLIC-AUDIO:", json.dumps(player, sort_keys=True), flush=True)
        downloads_state = fallback.browser_downloads(chrome_port)
        app_db = home / ".local" / "share" / "com.downloadmanager.app" / "download-manager.db"
        assert policy.get("policy", {}).get("showMediaButtons") is False, policy
        assert player["button"] is False, player
        assert player["readyState"] >= 2 and player["paused"] is False, player
        assert not downloads_state, downloads_state
        assert not app_db.exists(), app_db
        print(f"PACIello-MEDIA-OFF-CHROMIUM-PROBE: PASS (page={PAGE}, source={player['src']}, readyState={player['readyState']}, paused={player['paused']}, button={player['button']}, browser_downloads=0, native_jobs=0)", flush=True)
        return 0
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
