#!/usr/bin/env python3
"""Real Chromium proof that an explicit Save-As gesture stays browser-owned.

The extension's ordinary interception is limited to an unmodified left click.
This probe right-clicks a visible explicit ``<a download>`` and records the
trusted contextmenu event before its own listener prevents the headless browser
menu from opening. No download command is selected: the acceptance is that the
extension does not capture or prevent the Save-Link-As gesture.
"""
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
import browser_native_failure_fallback_chromium_probe as failure
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

EXTENSION = public.EXTENSION
CHROME = public.CHROME
DEV_EXTENSION_ID = public.DEV_EXTENSION_ID
FILENAME = "save-as-ownership.bin"


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-save-as-ownership-chromium-"))
    home = root / "home"
    profile = root / "profile"
    home.mkdir(parents=True)
    server = failure.DownloadServer()
    source = f"http://127.0.0.1:{server.port}/{FILENAME}"
    page = f"http://127.0.0.1:{server.port}/page"
    failure_log = failure.write_failure_manifest(home, profile, root)
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    chrome = None
    client = None
    try:
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
                page,
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        deadline = time.time() + 30
        last = None
        while time.time() < deadline:
            try:
                last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
                if last.get("href") == page and last.get("state") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError(f"Save-As page did not load: {last}")

        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
        native = diagnostic.get("native") if isinstance(diagnostic, dict) else None
        native_response = native.get("response") if isinstance(native, dict) else None
        if not isinstance(native_response, dict) or native_response.get("ok") is not True:
            raise RuntimeError(f"fake native host did not answer policy request: {diagnostic}")

        raw = client.evaluate(
            "JSON.stringify((()=>{"
            "const old=document.querySelector('#dm-save-as-link');old?.remove();"
            f"const a=document.createElement('a');a.id='dm-save-as-link';a.href={json.dumps(source)};"
            f"a.download={json.dumps(FILENAME)};a.textContent='Save link as';"
            "a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
            "document.body.append(a);window.__dmSaveAs={context:null};"
            "document.addEventListener('contextmenu',event=>{const target=event.target;"
            "if(target instanceof Element&&target.closest('#dm-save-as-link')){"
            "window.__dmSaveAs.context={isTrusted:event.isTrusted,button:event.button,defaultPrevented:event.defaultPrevented};"
            "event.preventDefault();}},true);"
            "const r=a.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,href:a.href,download:a.download};})())"
        )
        point = json.loads(raw)
        if point.get("download") != FILENAME:
            raise RuntimeError(f"explicit Save-As anchor was not constructed: {point}")
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": point["x"],
                "y": point["y"],
                "button": "right",
                "clickCount": 1,
                "modifiers": 0,
            },
        )
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": point["x"],
                "y": point["y"],
                "button": "right",
                "clickCount": 1,
                "modifiers": 0,
            },
        )
        context = json.loads(client.evaluate("JSON.stringify(window.__dmSaveAs.context)"))
        messages = [json.loads(line) for line in failure_log.read_text(encoding="utf-8").splitlines() if line.strip()] if failure_log.exists() else []
        print("SAVE-AS-CONTEXTMENU:", json.dumps(context, sort_keys=True), flush=True)
        print("FAKE-NATIVE-MESSAGES:", json.dumps(messages, sort_keys=True), flush=True)
        captures = [message for message in messages if message.get("type") == "capture-acquisition"]
        downloads = failure.browser_downloads(chrome_port)
        app_db = home / ".local" / "share" / "com.downloadmanager.app" / "download-manager.db"
        print("SAVE-AS-DOWNLOADS:", json.dumps(downloads, sort_keys=True), flush=True)
        assert context and context.get("isTrusted") is True and context.get("button") == 2 and context.get("defaultPrevented") is False, context
        assert not captures, messages
        assert not downloads, downloads
        assert not app_db.exists(), app_db
        print("SAVE-AS-OWNERSHIP-CHROMIUM: PASS (trusted_contextmenu=true, default_prevented=false, native_captures=0, browser_downloads=0, native_jobs=0)", flush=True)
        print("SAVE-AS-OWNERSHIP-CHROMIUM-PROBE: PASS", flush=True)
        return 0
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
        server.stop()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_SAVE_AS_OWNERSHIP") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SAVE-AS-OWNERSHIP-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
