#!/usr/bin/env python3
"""Real Chromium proof that an excluded site keeps browser ownership."""
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
import browser_fallback_chromium_probe as fallback
import public_chromium_probe as public
import segmented_restart_probe as support


def set_excluded_site(client, site: str) -> dict:
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
    raw = client.evaluate(
        "(async()=>await chrome.runtime.sendMessage({type:'update-policy',"
        f"patch:{{excludedSites:[{json.dumps(site)}]}}}}))()"
    )
    return raw if isinstance(raw, dict) else json.loads(raw)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-excluded-site-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    for directory in (home, profile, downloads):
        directory.mkdir(parents=True, exist_ok=True)
    server = fallback.DownloadServer()
    chrome = None
    client = None
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    source = f"http://127.0.0.1:{server.port}/browser-fallback.bin"
    page = f"http://127.0.0.1:{server.port}/excluded-page"
    try:
        chrome = subprocess.Popen(
            [str(fallback.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={fallback.EXTENSION}", f"--disable-extensions-except={fallback.EXTENSION}", "--window-size=1280,900", page],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        policy = set_excluded_site(client, "127.0.0.1")
        print("EXCLUDED-POLICY:", json.dumps(policy, sort_keys=True), flush=True)
        public.wait_page(client, page)
        point = fallback.click_download_link(client, source)
        print("TRUSTED-EXCLUDED-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        browser_record, browser_path = fallback.wait_browser_download(chrome_port, profile, source)
        browser_bytes = browser_path.read_bytes()
        browser_hash = hashlib.sha256(browser_bytes).hexdigest()
        expected_hash = hashlib.sha256(fallback.PAYLOAD).hexdigest()
        browser_files = [browser_record["filename"]]
        assert len(browser_bytes) == len(fallback.PAYLOAD), len(browser_bytes)
        assert browser_hash == expected_hash, (browser_hash, expected_hash)
        assert not browser_record.get("byExtensionId"), browser_record
        assert len(browser_files) == 1, browser_files
        assert server.requests == 1, server.requests
        print(f"EXCLUDED-SITE-CHROMIUM-PROBE: PASS (site=127.0.0.1, browser_bytes={len(browser_bytes)}, sha256={browser_hash}, browser_initiator=page, downloads={browser_files}, source_requests={server.requests}, native_jobs=0)", flush=True)
        return 0
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill()
        server.stop()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
