#!/usr/bin/env python3
"""Real Chromium proof: cross-origin Referer is stripped to the page origin.

SPEC §16: paths and query strings must never leak cross-origin. The page
lives on 127.0.0.1 while the file is fetched from localhost (different
origin). The fixture's /file/origin-gated.bin serves only when the Referer
is exactly the page origin — a full page URL must 403, and so must a bare
fetch. The probe asserts all three gate behaviors plus a completed native
job with byte/hash equality.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, referer: str | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if referer is not None:
        request.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def wait_terminal_job(db: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            items = [job for job in public.jobs(db) if not job.get("media")]
            if items and items[-1].get("state") in ("completed", "failed"):
                return items[-1]
            if items:
                last = items[-1]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"no terminal ordinary job appeared: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-xorigin-scope-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "fixtures/server.py", "--port", str(port)],
        cwd="/srv/repos/downloadmanager",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    page_url = f"http://127.0.0.1:{port}/page/video.html"
    page_origin = f"http://127.0.0.1:{port}"
    file_url = f"http://localhost:{port}/file/origin-gated.bin"
    # Gate discrimination: bare and full-URL Referers 403; origin passes.
    assert fetch(file_url, None)[0] == 403, "gate open without Referer"
    assert fetch(file_url, page_url)[0] == 403, "gate accepts full page URL"
    status, expected = fetch(file_url, page_origin)
    assert status == 200 and len(expected) == 1048576, (status, len(expected))
    expected_hash = hashlib.sha256(expected).hexdigest()
    print(f"ORIGIN-GATE: bare=403 full-url=403 origin=200 bytes={len(expected)} sha256={expected_hash}", flush=True)
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
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
             f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
             f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
             "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, page_url)
        client.evaluate(
            "document.body.insertAdjacentHTML('beforeend',"
            f"`<a id='xo' href={json.dumps(file_url)} download='author-name.bin'>xo</a>`)"
        )
        state = json.loads(client.evaluate(
            "JSON.stringify((()=>{const a=document.querySelector('#xo');"
            "return {cross:new URL(a.href).origin!==new URL(location.href).origin};})())"
        ))
        assert state["cross"] is True, state
        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const a=document.querySelector('#xo');"
            "a.scrollIntoView({block:'center'});const r=a.getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
        ))
        for kind, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
            client.call("Input.dispatchMouseEvent", {"type": kind, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        deadline = time.time() + 60
        job = None
        while time.time() < deadline:
            items = [item for item in public.jobs(db) if not item.get("media")]
            if items:
                job = items[-1]
                break
            time.sleep(0.5)
        if not job:
            raise RuntimeError(f"capture created no ordinary job: {public.jobs(db)}")
        print("XORIGIN-SCOPE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "name": job.get("name")}, sort_keys=True), flush=True)
        # Both fixes compose: cross-origin author name ignored AND origin Referer sent.
        assert job.get("name") == "origin-gated.bin", job
        managed = managed_dir / "origin-gated.bin"
        public.commit_via_cli(str(home), inspector_port, job["id"], "origin-gated.bin", str(managed))
        done = wait_terminal_job(db)
        print("XORIGIN-SCOPE-TERMINAL:", json.dumps({"state": done.get("state"), "error": done.get("error")}, sort_keys=True), flush=True)
        assert done.get("state") == "completed", done
        assert done.get("provisional") is False, done
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        assert native_size == len(expected) and native_hash == expected_hash, (native_size, native_hash)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"XORIGIN-SCOPE: PASS (output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("XORIGIN-SCOPE-PROBE: PASS", flush=True)
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
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        if server.poll() is None:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"XORIGIN-SCOPE-PROBE: FAIL: {error}", flush=True)
        raise
