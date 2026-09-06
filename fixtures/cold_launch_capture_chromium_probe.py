#!/usr/bin/env python3
"""Real Chromium proof: browser capture cold-starts the resident app.

SPEC §19.1 / M2 launch-from-extension: with no resident process running, a
browser capture must start the app, create the provisional acquisition, show
the small standalone Add Download window — and NOT unnecessarily open the
manager. Chrome spawns `BIN --native-host` on demand (manifest wrapper);
that forwards `--capture` to a fresh resident whose setup hides main and
starts the provisional.

Observables (all read back, none trusted):
- no app process for the disposable HOME before the click (environ scan);
- after the click: a new app process whose environ contains the HOME root,
  a database with exactly one provisional ordinary job for the file, and an
  empty Chromium Downloads directory (no browser fallback);
- X11 visibility under the test DISPLAY: >=1 visible "Add Download" window
  and zero visible "Download Manager" main windows, sustained over 3
  consecutive 1s samples taken after the Add window first appears.
"""
from __future__ import annotations

import json
import os
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def app_pids_for(root: str) -> list[int]:
    """PIDs whose executable is the resident binary and whose environ holds root."""
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            if Path(exe).name != Path(BIN).name:
                continue
            env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
            if any(root.encode() in entry for entry in env):
                found.append(int(pid))
        except Exception:
            continue
    return found


def wait_provisional_job(db: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: list = []
    while time.time() < deadline:
        try:
            last = [job for job in public.jobs(db) if job.get("provisional") is True and not job.get("media")]
            if last:
                return last[-1]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"cold capture created no provisional ordinary job: {last}")


def x11_visible(display: str, pattern: str) -> list[str]:
    out = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", pattern],
        capture_output=True, text=True, env={**os.environ, "DISPLAY": display}, timeout=15,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-cold-launch-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "fixtures/server.py", "--port", str(port)],
        cwd="/srv/repos/downloadmanager",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    file_url = f"http://127.0.0.1:{port}/file/range.bin"
    with urllib.request.urlopen(file_url, timeout=30) as response:
        expected = response.read()
    assert len(expected) > 0
    before = app_pids_for(str(root))
    assert not before, before
    print(f"COLD-LAUNCH-PRE: app_pids={before}", flush=True)
    public.write_native_manifest(str(home), str(profile))
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    chrome = None
    client = None
    try:
        # NOTE: the resident is deliberately NOT started here.
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
             f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
             f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
             "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port := support.free_port()),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, f"http://127.0.0.1:{port}/page/video.html")
        client.evaluate(
            "document.body.insertAdjacentHTML('beforeend',"
            f"`<a id='cold' href={json.dumps(file_url)} download='cold-launch.bin'>cold</a>`)"
        )
        point = json.loads(client.evaluate(
            "JSON.stringify((()=>{const a=document.querySelector('#cold');"
            "a.scrollIntoView({block:'center'});const r=a.getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};})())"
        ))
        for kind, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
            client.call("Input.dispatchMouseEvent", {"type": kind, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = wait_provisional_job(db)
        print("COLD-LAUNCH-JOB:", json.dumps({"id": job["id"], "source": job["source"], "name": job.get("name"), "state": job.get("state"), "provisional": job.get("provisional")}, sort_keys=True), flush=True)
        assert job.get("source") == file_url, job
        deadline = time.time() + 60
        spawned: list[int] = []
        while time.time() < deadline:
            spawned = [pid for pid in app_pids_for(str(root))]
            if spawned:
                break
            time.sleep(0.5)
        assert spawned, "no resident process appeared for the disposable HOME"
        print(f"COLD-LAUNCH-APP: pids={spawned}", flush=True)
        # Window visibility: Add present, manager absent, sustained 3x1s.
        samples = []
        deadline = time.time() + 90
        while time.time() < deadline:
            add_ids = x11_visible(display, "^Add Download$")
            main_ids = x11_visible(display, "^Download Manager$")
            if add_ids and not main_ids:
                samples.append({"add": len(add_ids), "main": len(main_ids)})
                if len(samples) >= 3:
                    break
            else:
                samples.clear()
            time.sleep(1.0)
        print("COLD-LAUNCH-WINDOWS:", json.dumps(samples[-3:], sort_keys=True), flush=True)
        assert len(samples) >= 3 and all(s["add"] >= 1 and s["main"] == 0 for s in samples), samples
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        assert len([j for j in public.jobs(db) if not j.get("media")]) == 1, public.jobs(db)
        print(f"COLD-LAUNCH: PASS (spawned_pids={spawned}, job={job['id']}, name={job.get('name')}, add_window_visible=true, manager_visible=false, jobs=1, browser_downloads={browser_files})", flush=True)
        print("COLD-LAUNCH-PROBE: PASS", flush=True)
        return 0
    except Exception:
        try:
            tree = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True, env={**os.environ, "DISPLAY": display}, timeout=15)
            print("XWIN-TREE:", tree.stdout[-3000:], flush=True)
        except Exception as error:
            print(f"XWIN-TREE-UNAVAILABLE: {error!r}", flush=True)
        raise
    finally:
        if client is not None:
            try: client.sock.close()
            except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill(); chrome.wait(timeout=10)
        for pid in app_pids_for(str(root)):
            try:
                subprocess.run(["kill", str(pid)], timeout=5)
            except Exception:
                pass
        if server.poll() is None:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"COLD-LAUNCH-PROBE: FAIL: {error}", flush=True)
        raise
