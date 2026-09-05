#!/usr/bin/env python3
"""Real native/UI proof for failed-job notifications."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_fallback_chromium_probe as fallback
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN


def wait_job(db: str, source: str, predicate, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rows = public.jobs(db)
        matching = [row for row in rows if row.get("source") == source]
        if matching:
            last = matching[-1]
            if predicate(last):
                return last
        time.sleep(0.25)
    raise RuntimeError(f"job did not reach expected state: {last}")


def wait_surface(client: support.WebKitClient, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = client.evaluate(
                "JSON.stringify({href:location.href,ready:document.readyState,"
                "surface:!!document.querySelector('.notifications-surface'),"
                "cards:document.querySelectorAll('.notification-card').length,"
                "buttons:Array.from(document.querySelectorAll('.notification-card button')).map(button=>button.textContent.trim()).filter(Boolean),"
                "text:document.querySelector('.notifications-surface')?.innerText||''})"
            )
            import json as _json
            last = _json.loads(raw)
            if last.get("surface") and last.get("cards", 0) >= 1:
                return last
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Notifications surface did not render: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-failure-notification-"))
    home = root / "home"
    profile = root / "chrome-profile"
    downloads = root / "downloads"
    managed_dir = root / "managed"
    for directory in (home, profile, downloads, managed_dir):
        directory.mkdir(parents=True, exist_ok=True)
    server = fallback.DownloadServer()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    inspector_port = support.free_port()
    env = support.app_env(str(home), inspector_port)
    app = None
    chrome = None
    client = None
    app_client = None
    try:
        app_log_path = root / "app.log"
        app_log = app_log_path.open("w")
        app = subprocess.Popen([BIN], env=env, stdin=subprocess.DEVNULL, stdout=app_log, stderr=app_log)
        support.wait_inspector(inspector_port, timeout=30).close()
        support.wait_db(str(home), timeout=30)
        chrome_port = support.free_port()
        browser_env = public.browser_env(str(home), inspector_port)
        public.write_native_manifest(str(home), str(profile))
        chrome = subprocess.Popen(
            [str(fallback.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]
            + [f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={fallback.EXTENSION}", f"--disable-extensions-except={fallback.EXTENSION}", "--window-size=1280,900", f"http://127.0.0.1:{server.port}/missing-page"],
            env=browser_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        source = f"http://127.0.0.1:{server.port}/missing-safe-test.bin"
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, f"http://127.0.0.1:{server.port}/missing-page")
        client.evaluate(
            "(() => { const a=document.createElement('a'); a.id='missing'; "
            "a.href=" + json.dumps(source) + "; a.download='failure.bin'; a.textContent='Download'; "
            "a.style='display:block;width:220px;height:50px'; document.body.appendChild(a); "
            "const r=a.getBoundingClientRect(); return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2}); })()"
        )
        point = json.loads(client.evaluate("JSON.stringify((()=>{const r=document.querySelector('#missing').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})())"))
        client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
        db = public.db_path(str(home))
        failed = wait_job(db, source, lambda job: job.get("state") == "failed", timeout=90)
        print("FAILED-JOB:", json.dumps({"id": failed["id"], "source": failed["source"], "state": failed["state"], "provisional": failed.get("provisional"), "error": failed.get("error")}, sort_keys=True), flush=True)
        subprocess.run([BIN], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        app_client = support.wait_inspector(inspector_port, timeout=30)
        app_client.evaluate("(window.location.href=window.location.pathname+'?view=notifications','navigated')")
        surface = wait_surface(app_client)
        print("NOTIFICATIONS-SURFACE:", json.dumps(surface, sort_keys=True), flush=True)
        assert surface["cards"] == 1, surface
        assert "Download failed" in surface["text"], surface
        assert "failure.bin" in surface["text"], surface
        assert surface["buttons"] == ["View details", "Open Manager"], surface
        assert not list(managed_dir.iterdir()), list(managed_dir.iterdir())
        assert server.requests == 0, server.requests
        print(f"FAILURE-NOTIFICATION-PROBE: PASS (job={failed['id']}, state=failed, cards={surface['cards']}, buttons={surface['buttons']}, source_requests={server.requests}, managed_files=0)", flush=True)
        return 0
    except Exception as exc:
        print(f"FAILURE-NOTIFICATION-PROBE: FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        for browser_client in (client, app_client):
            if browser_client is not None:
                try: browser_client.close()
                except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill()
        if app is not None and app.poll() is None:
            app.terminate()
            try: app.wait(timeout=10)
            except subprocess.TimeoutExpired: app.kill()
        xvfb.terminate()
        try: xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired: xvfb.kill()
        server.stop()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
