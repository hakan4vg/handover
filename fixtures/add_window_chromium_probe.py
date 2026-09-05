#!/usr/bin/env python3
"""Real Chromium/native proof that capture opens the Add Download surface."""
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
    raise RuntimeError(f"capture did not create a native job: {last}")


def add_window_tree(env: dict[str, str]) -> str:
    result = subprocess.run(["xwininfo", "-root", "-tree"], env=env, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"xwininfo failed: {result.stderr.strip()}")
    return result.stdout


def wait_add_window(env: dict[str, str], timeout: float = 30.0) -> tuple[str, list[str]]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = add_window_tree(env)
        lines = [line.strip() for line in last.splitlines() if '"Add Download"' in line]
        if lines:
            return last, lines
        time.sleep(0.2)
    raise RuntimeError(f"Add Download window did not become visible: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-add-window-chromium-"))
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
    env = public.browser_env(str(home), inspector_port)
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=env, stdout=app_log, stderr=subprocess.STDOUT)
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
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
            raise RuntimeError(f"page did not load: {last}")
        point = fallback.click_download_link(client, source)
        print("TRUSTED-ANCHOR-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        db = public.db_path(str(home))
        job = wait_native_job(db, source)
        assert job.get("provisional") is True, job
        tree, windows = wait_add_window(env)
        print("ADD-WINDOW:", json.dumps({"matches": windows, "tree": tree}, sort_keys=True), flush=True)
        managed_name = "add-window-confirmation.bin"
        managed = managed_dir / managed_name
        public.commit_via_cli(str(home), inspector_port, job["id"], managed_name, str(managed))
        done = public.wait_completed(db, job["id"], timeout=180)
        native_bytes = managed.read_bytes()
        native_hash = hashlib.sha256(native_bytes).hexdigest()
        browser_size, browser_hash = public.sha256_browser(client, source)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(public.jobs(db)) == 1, public.jobs(db)
        assert len(native_bytes) == browser_size, (len(native_bytes), browser_size, done)
        assert native_hash == browser_hash, (native_hash, browser_hash, done)
        assert not browser_files, browser_files
        assert server.requests == 2, server.requests
        print(f"ADD-WINDOW-CHROMIUM: PASS (source={source}, output_bytes={len(native_bytes)}, output_sha256={native_hash}, jobs=1, source_requests={server.requests}, browser_downloads={browser_files}, ui_matches={len(windows)})", flush=True)
        print("ADD-WINDOW-CHROMIUM-PROBE: PASS", flush=True)
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
        if os.environ.get("DM_KEEP_ADD_WINDOW") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ADD-WINDOW-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
