#!/usr/bin/env python3
"""Real Chromium proof that interception-off leaves a public download alone."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME
PAGE = "https://www.w3schools.com/html/html5_video.asp"
DOWNLOAD_URL = "https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_checksums.txt"
FILENAME = "gh_2.100.0_checksums.txt"


def redacted_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}" + ("?[REDACTED]" if parsed.query else "")


def worker_client(port: int) -> public.cdp_drive.CDP:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    worker = next(
        item for item in targets
        if item.get("type") == "service_worker" and item.get("url", "").endswith("/background.js")
    )
    path = worker["webSocketDebuggerUrl"].split(f"127.0.0.1:{port}", 1)[1]
    client = public.cdp_drive.CDP(public.cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    return client


def set_interception_off(client: public.cdp_drive.CDP) -> dict:
    extension_page = f"chrome-extension://{public.DEV_EXTENSION_ID}/popup.html"
    client.call("Page.navigate", {"url": extension_page})
    deadline = time.time() + 30
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
            if last.get("href") == extension_page and last.get("state") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError(f"extension page did not load: {last}")
    raw = client.evaluate(
        "(async()=>await chrome.runtime.sendMessage({type:'update-policy',patch:{interceptDownloads:false}}))()"
    )
    response = raw if isinstance(raw, dict) else json.loads(raw)
    if response.get("policy", {}).get("interceptDownloads") is not False:
        raise RuntimeError(f"extension page did not disable interception: {response}")
    return response


def browser_downloads(port: int) -> list[dict]:
    client = worker_client(port)
    try:
        raw = client.evaluate("(async()=>JSON.stringify(await chrome.downloads.search({limit:50})))()")
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    finally:
        client.sock.close()


def matching(records: list[dict]) -> list[dict]:
    return [
        item for item in records
        if FILENAME in (
            str(item.get("filename", ""))
            + str(item.get("url", ""))
            + str(item.get("finalUrl", ""))
        )
    ]


def wait_browser_download(port: int, profile: Path, timeout: float = 90.0) -> tuple[dict, Path]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = browser_downloads(port)
        except Exception:
            last = []
        complete = [item for item in matching(last) if item.get("state") == "complete" and not item.get("error")]
        if len(complete) == 1:
            path = Path(complete[0]["filename"])
            if path.exists() and path.stat().st_size > 0:
                return complete[0], path
        time.sleep(0.25)
    raise RuntimeError(f"public browser download did not complete exactly once: {last}")


def click_link(client: public.cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-public-integration-off-link');old?.remove();"
        f"const a=document.createElement('a');a.id='dm-public-integration-off-link';a.href={json.dumps(DOWNLOAD_URL)};"
        f"a.download={json.dumps(FILENAME)};a.textContent='Browser-owned public download';"
        "a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);const r=a.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,href:a.href,download:a.download};})())"
    )
    point = json.loads(raw)
    for event_type in ("mousePressed", "mouseReleased"):
        client.call(
            "Input.dispatchMouseEvent",
            {"type": event_type, "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0},
        )
    return point


def native_processes() -> list[str]:
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if cmdline and cmdline[0] == str(BIN).encode():
            found.append(str(entry))
    return found


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-integration-off-"))
    home = root / "home"
    profile = root / "profile"
    downloads = home / "Downloads"
    home.mkdir(parents=True)
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    chrome = None
    client = None
    try:
        chrome = subprocess.Popen(
            [
                str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,900", PAGE,
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        policy = set_interception_off(client)
        print("POLICY-OFF:", json.dumps(policy, sort_keys=True), flush=True)
        public.wait_page(client, PAGE)
        point = click_link(client)
        print("TRUSTED-PUBLIC-CLICK:", json.dumps(point, sort_keys=True), flush=True)
        record, path = wait_browser_download(chrome_port, profile)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        print(
            "BROWSER-OWNED-PUBLIC-DOWNLOAD:",
            json.dumps(
                {
                    "id": record.get("id"),
                    "state": record.get("state"),
                    "error": record.get("error"),
                    "byExtensionId": record.get("byExtensionId"),
                    "url": redacted_url(record.get("url")),
                    "finalUrl": redacted_url(record.get("finalUrl")),
                    "filename": record.get("filename"),
                    "bytesReceived": record.get("bytesReceived"),
                    "size": len(data),
                    "sha256": digest,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        time.sleep(1)
        records = matching(browser_downloads(chrome_port))
        app_db = Path(public.db_path(str(home)))
        processes = native_processes()
        assert policy["policy"]["interceptDownloads"] is False, policy
        assert len(records) == 1, records
        assert record.get("state") == "complete" and not record.get("error"), record
        assert not record.get("byExtensionId"), record
        assert path == downloads / FILENAME, path
        assert path.stat().st_size == len(data) and len(data) > 0
        assert not app_db.exists(), app_db
        assert not processes, processes
        print(
            f"PUBLIC-INTEGRATION-OFF: PASS (browser_bytes={len(data)}, sha256={digest}, "
            f"downloads={len(records)}, browser_initiator=public-page, native_jobs=0, native_processes=0)",
            flush=True,
        )
        print("PUBLIC-INTEGRATION-OFF-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        raise
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
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_PUBLIC_INTEGRATION_OFF") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-INTEGRATION-OFF-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
