#!/usr/bin/env python3
"""Real Chromium/native proof for explicit <a download> interception."""
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
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME
FILENAME = "browser-fallback.bin"


def wait_native_job(db: str, source: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in public.jobs(db) if job.get("source") == source]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"native explicit-anchor capture did not create a job: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-explicit-anchor-chromium-"))
    home = root / "home"
    profile = root / "profile"
    managed_dir = root / "Managed"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    server = fallback.DownloadServer()
    source = f"http://127.0.0.1:{server.port}/{FILENAME}"
    page = f"http://127.0.0.1:{server.port}/page"
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
        chrome = subprocess.Popen(
            [
                str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,900", page,
            ],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
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
            raise RuntimeError(f"explicit-anchor page did not load: {last}")
        point = fallback.click_download_link(client, source)
        print("TRUSTED-ANCHOR-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        db = public.db_path(str(home))
        job = wait_native_job(db, source)
        assert job.get("media") is False, job
        print("NATIVE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "media": job.get("media"), "state": job.get("state"), "provisional": job.get("provisional"), "name": job.get("name")}, sort_keys=True), flush=True)
        managed_name = "explicit-anchor" + Path(job["source"]).suffix
        managed = managed_dir / managed_name
        public.commit_via_cli(str(home), inspector_port, job["id"], managed_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=180)
        native_bytes = managed.read_bytes()
        native_hash = hashlib.sha256(native_bytes).hexdigest()
        browser_size, browser_hash = public.sha256_browser(client, source)
        print("BROWSER-REFERENCE:", json.dumps({"size": browser_size, "hash": browser_hash, "source": source}, sort_keys=True), flush=True)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(public.jobs(db)) == 1, public.jobs(db)
        assert len(native_bytes) == browser_size, (len(native_bytes), browser_size, done)
        assert native_hash == browser_hash, (native_hash, browser_hash, done)
        assert not browser_files, browser_files
        assert server.requests == 2, server.requests
        print(f"EXPLICIT-ANCHOR-CHROMIUM: PASS (source={source}, output_bytes={len(native_bytes)}, output_sha256={native_hash}, jobs=1, source_requests={server.requests}, browser_downloads={browser_files})", flush=True)
        print("EXPLICIT-ANCHOR-CHROMIUM-PROBE: PASS", flush=True)
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
        server.stop()
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_EXPLICIT_ANCHOR") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"EXPLICIT-ANCHOR-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
