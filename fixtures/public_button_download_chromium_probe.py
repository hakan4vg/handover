#!/usr/bin/env python3
"""Real Chromium proof for a public button-triggered ordinary download.

The trusted user gesture targets a page button. Its handler creates an anchor
without a ``download`` attribute and clicks it programmatically. This is a
browser-owned initiation shape outside the content script's explicit-anchor
pre-browser path, so the existing observe-only downloads fallback must create
one native provisional job without cancelling Chromium's own copy.
"""
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
DOWNLOAD_URL = "https://github.com/cli/cli/archive/refs/tags/v2.100.0.tar.gz"
DOWNLOAD_FINAL_PATH = "/cli/cli/tar.gz/refs/tags/v2.100.0"
DOWNLOAD_NAME = "cli-2.100.0.tar.gz"


def redacted_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}" + ("?[REDACTED]" if parsed.query else "")


def source_matches(source: object) -> bool:
    if not isinstance(source, str):
        return False
    return (
        DOWNLOAD_URL in source
        or ("codeload.github.com/cli/cli" in source and DOWNLOAD_FINAL_PATH in source)
    )


def jobs(db: str) -> list[dict]:
    return public.jobs(db)


def wait_native_job(db: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if source_matches(job.get("source"))]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"button download did not create a native job: {last}")


def wait_completed(db: str, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = next((job for job in jobs(db) if job.get("id") == job_id), None)
        if last and last.get("state") == "completed" and last.get("provisional") is False:
            return last
        if last and last.get("state") == "failed":
            raise RuntimeError(
                "button native job failed: "
                + json.dumps(
                    {key: last.get(key) for key in ("id", "state", "error", "source")},
                    default=str,
                )
            )
        time.sleep(0.5)
    raise RuntimeError(f"button native job did not complete: {last}")


def worker_downloads(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    worker = next(
        (
            item
            for item in targets
            if item.get("type") == "service_worker"
            and item.get("url", "").endswith("/background.js")
        ),
        None,
    )
    if worker is None:
        return []
    path = worker["webSocketDebuggerUrl"].split(f"127.0.0.1:{port}", 1)[1]
    client = public.cdp_drive.CDP(public.cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    try:
        raw = client.evaluate("(async()=>JSON.stringify(await chrome.downloads.search({limit:50})))()")
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    finally:
        client.sock.close()


def matching_downloads(records: list[dict]) -> list[dict]:
    return [
        item
        for item in records
        if DOWNLOAD_NAME in (
            str(item.get("filename", ""))
            + str(item.get("url", ""))
            + str(item.get("finalUrl", ""))
        )
        or (
            source_matches(item.get("url"))
            or source_matches(item.get("finalUrl"))
        )
    ]


def wait_browser_download(port: int, timeout: float = 240.0) -> tuple[dict, Path]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = worker_downloads(port)
        except Exception:
            last = []
        matches = matching_downloads(last)
        complete = [item for item in matches if item.get("state") == "complete" and not item.get("error")]
        if len(complete) == 1:
            path = Path(complete[0]["filename"])
            if path.exists() and path.stat().st_size > 0:
                return complete[0], path
        time.sleep(0.5)
    raise RuntimeError(f"button browser download did not complete exactly once: {last}")


def install_button(client: public.cdp_drive.CDP) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-public-button-download');old?.remove();"
        "const button=document.createElement('button');button.id='dm-public-button-download';"
        "button.type='button';button.textContent='Download public archive';"
        "button.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000;font:16px sans-serif';"
        "button.addEventListener('click',()=>{"
        f"const anchor=document.createElement('a');anchor.href={json.dumps(DOWNLOAD_URL)};"
        "anchor.textContent='generated archive';document.body.append(anchor);anchor.click();"
        "setTimeout(()=>anchor.remove(),2000);"
        "});document.body.append(button);button.focus();"
        "const r=button.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,text:button.textContent,type:button.type,"
        "hasDownload:button.hasAttribute('download'),anchorDownload:false};})())"
    )
    point = json.loads(raw)
    if point.get("type") != "button" or point.get("hasDownload") or point.get("anchorDownload"):
        raise RuntimeError(f"button initiation fixture metadata mismatch: {point}")
    return point


def click_button(client: public.cdp_drive.CDP, point: dict) -> None:
    for event_type, button, buttons in (
        ("mouseMoved", "none", 0),
        ("mousePressed", "left", 1),
        ("mouseReleased", "left", 0),
    ):
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": point["x"],
                "y": point["y"],
                "button": button,
                "buttons": buttons,
                "clickCount": 1,
                "modifiers": 0,
            },
        )


def safe_job(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "state": job.get("state"),
        "provisional": job.get("provisional"),
        "source": redacted_url(job.get("source")),
        "error": job.get("error"),
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-button-download-"))
    home = root / "home"
    profile = root / "profile"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen(
        [BIN],
        env=public.browser_env(str(home), inspector_port),
        stdout=app_log,
        stderr=subprocess.STDOUT,
    )
    chrome = None
    client = None
    try:
        db = public.db_path(str(home))
        support.wait_db(str(home))
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
                "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, PAGE)
        diagnostic = public.extension_diagnostic(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(diagnostic, sort_keys=True), flush=True)
        native_policy = ((diagnostic.get("native") or {}).get("response") or {}).get("policy") or {}
        if native_policy.get("interceptDownloads") is not True:
            raise RuntimeError(f"fresh browser policy was not interception-on: {diagnostic}")
        point = install_button(client)
        print("TRUSTED-BUTTON:", json.dumps(point, sort_keys=True), flush=True)
        click_button(client, point)
        native_job = wait_native_job(db)
        print("BUTTON-NATIVE-JOB:", json.dumps(safe_job(native_job), sort_keys=True), flush=True)
        browser_record, browser_path = wait_browser_download(chrome_port)
        browser_bytes = browser_path.read_bytes()
        browser_hash = hashlib.sha256(browser_bytes).hexdigest()
        print(
            "BUTTON-BROWSER-DOWNLOAD:",
            json.dumps(
                {
                    "id": browser_record.get("id"),
                    "state": browser_record.get("state"),
                    "error": browser_record.get("error"),
                    "byExtensionId": browser_record.get("byExtensionId"),
                    "url": redacted_url(browser_record.get("url")),
                    "finalUrl": redacted_url(browser_record.get("finalUrl")),
                    "filename": browser_record.get("filename"),
                    "bytesReceived": browser_record.get("bytesReceived"),
                    "size": len(browser_bytes),
                    "sha256": browser_hash,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        destination = root / "Managed" / DOWNLOAD_NAME
        public.commit_via_cli(str(home), inspector_port, native_job["id"], DOWNLOAD_NAME, str(destination))
        native_done = wait_completed(db, native_job["id"])
        native_bytes = destination.read_bytes()
        native_hash = hashlib.sha256(native_bytes).hexdigest()
        print(
            "BUTTON-NATIVE-DOWNLOAD:",
            json.dumps(
                {
                    "state": native_done.get("state"),
                    "provisional": native_done.get("provisional"),
                    "name": native_done.get("name"),
                    "source": redacted_url(native_done.get("source")),
                    "size": len(native_bytes),
                    "sha256": native_hash,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        with urllib.request.urlopen(DOWNLOAD_URL, timeout=120) as response:
            reference_url = response.geturl()
            reference_bytes = response.read()
        reference_hash = hashlib.sha256(reference_bytes).hexdigest()
        print(
            "BUTTON-INDEPENDENT-REFERENCE:",
            json.dumps(
                {
                    "url": redacted_url(reference_url),
                    "size": len(reference_bytes),
                    "sha256": reference_hash,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        records = matching_downloads(worker_downloads(chrome_port))
        product_jobs = jobs(db)
        assert browser_record.get("state") == "complete" and not browser_record.get("error"), browser_record
        assert not browser_record.get("byExtensionId"), browser_record
        assert Path(browser_record["filename"]).name == DOWNLOAD_NAME, browser_record
        assert native_done.get("name") == DOWNLOAD_NAME, native_done
        assert len(product_jobs) == 1, product_jobs
        assert len(records) == 1, records
        assert len(browser_bytes) == len(native_bytes) == len(reference_bytes) > 0
        assert browser_hash == native_hash == reference_hash, (browser_hash, native_hash, reference_hash)
        assert native_done.get("state") == "completed" and native_done.get("provisional") is False, native_done
        print(
            f"PUBLIC-BUTTON-DOWNLOAD: PASS (browser_bytes={len(browser_bytes)}, browser_sha256={browser_hash}, "
            f"native_bytes={len(native_bytes)}, native_sha256={native_hash}, reference_bytes={len(reference_bytes)}, "
            f"reference_sha256={reference_hash}, browser_records={len(records)}, native_jobs={len(product_jobs)}, "
            "button_initiation=true, explicit_anchor_download=false)",
            flush=True,
        )
        print("PUBLIC-BUTTON-DOWNLOAD-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print("APP-LOG:", app_log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
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
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_PUBLIC_BUTTON_DOWNLOAD") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-BUTTON-DOWNLOAD-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
