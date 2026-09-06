#!/usr/bin/env python3
"""Real Chromium proof: cross-origin download attribute is not honored.

Chromium drops the author-supplied filename for cross-origin download
targets and saves under the server basename instead. The extension's
document-capture path must mirror that: a trusted click on a cross-origin
`<a download="author-name">` must create the native job under the URL
basename, not the author name — while same-origin valued attributes keep
working (covered by the jcisaacs probes).

Two local origins stand in for two public hosts: the page is served from
127.0.0.1 while the file is fetched from localhost (different host =
different origin), both on the disposable fixture server. No public host
is needed because the behavior under test is the generic origin comparison,
not any site's markup.
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
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
AUTHOR_NAME = "author-supplied-name.bin"
SERVER_NAME = "server-file.bin"


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


def wait_job(db: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = [job for job in public.jobs(db) if not job.get("media")]
        if items:
            return items[-1]
        time.sleep(0.2)
    raise RuntimeError(f"no ordinary native job appeared: {public.jobs(db)}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-xorigin-attr-"))
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
    payload = b"cross-origin-attribute-boundary-" * 1024
    reference_hash = hashlib.sha256(payload).hexdigest()
    page = (
        f"http://127.0.0.1:{port}/xorigin-page.html"
    )
    # The fixture server serves /file/range.bin deterministically; fetch it
    # once over the cross-origin host so the test does not depend on page
    # markup that the server does not ship. The anchor itself is created
    # top-document by the probe (boundary check, not site coverage).
    file_url = f"http://localhost:{port}/file/range.bin"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/file/range.bin", timeout=30) as response:
        same_origin_bytes = response.read()
    assert len(same_origin_bytes) > 0
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
        public.wait_page(client, f"http://127.0.0.1:{port}/page/video.html")
        state = json.loads(client.evaluate(
            "JSON.stringify((()=>{"
            f"document.body.innerHTML=`<a id='x' href={json.dumps(file_url)} download={json.dumps(AUTHOR_NAME)}>x</a>`;"
            "const a=document.querySelector('#x');"
            "return {href:a.href,origin:new URL(a.href).origin,pageOrigin:new URL(location.href).origin,"
            "cross:new URL(a.href).origin!==new URL(location.href).origin};})())"
        ))
        print("XORIGIN-SETUP:", json.dumps(state, sort_keys=True), flush=True)
        assert state["cross"] is True, state
        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const a=document.querySelector('#x');"
            "a.scrollIntoView({block:'center'});const r=a.getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
        ))
        for kind, btn in (("mouseMoved", "none"), ("mousePressed", "left"), ("mouseReleased", "left")):
            client.call("Input.dispatchMouseEvent", {"type": kind, "x": point["x"], "y": point["y"], "button": btn if btn != "none" else "none", "buttons": 1 if btn == "left" else 0, "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_job(db)
        print("XORIGIN-NATIVE-JOB:", json.dumps({"id": job["id"], "source": job["source"], "name": job.get("name"), "state": job.get("state")}, sort_keys=True), flush=True)
        if job.get("name") != SERVER_NAME and job.get("name") != "range.bin":
            raise RuntimeError(f"native job honored the cross-origin author name: {job}")
        managed = managed_dir / job["name"]
        public.commit_via_cli(str(home), inspector_port, job["id"], job["name"], str(managed))
        done = public.wait_completed(db, job["id"], timeout=120)
        native_size = managed.stat().st_size
        native_hash = sha256(managed)
        with urllib.request.urlopen(file_url, timeout=60) as response:
            expected = response.read()
        assert native_size == len(expected), (native_size, len(expected))
        assert native_hash == hashlib.sha256(expected).hexdigest(), native_hash
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        _ = (payload, reference_hash)
        print(f"XORIGIN-ATTR: PASS (author={AUTHOR_NAME}, native_name={job['name']}, output_bytes={native_size}, output_sha256={native_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("XORIGIN-ATTR-PROBE: PASS", flush=True)
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
        if server.poll() is None:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"XORIGIN-ATTR-PROBE: FAIL: {error}", flush=True)
        raise
