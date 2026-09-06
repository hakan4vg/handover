#!/usr/bin/env python3
"""Real Chromium ordinary-download ownership proof for a public redirect.

The public GitHub release URL redirects to a signed release-assets URL at
runtime. This probe deliberately runs two trusted Chromium cases in isolated
profiles:

1. a plain link, which exercises the current observe-only downloads fallback;
2. an explicit ``<a download>`` link, which exercises the generic pre-browser
   capture path.

The output keeps signed redirect query values redacted. The public case is the
product evidence; a separate disposable one-use probe may use the repository's
fixture server only to test the timing boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
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

DOWNLOAD_URL = "https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_checksums.txt"
DOWNLOAD_MATCH = "gh_2.100.0_checksums.txt"
DOWNLOAD_NAME = "gh_2.100.0_checksums.txt"
PAGE = "https://www.w3schools.com/html/html5_video.asp"
DEV_EXTENSION_ID = public.DEV_EXTENSION_ID
BIN = public.BIN
EXTENSION = public.EXTENSION
CHROME = public.CHROME


def db_path(home: str) -> str:
    return public.db_path(home)


def jobs(db: str) -> list[dict]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT payload FROM jobs ORDER BY created_at").fetchall()
    return [json.loads(payload) for (payload,) in rows]


def redacted_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    path = parsed.path or "/"
    suffix = "?[REDACTED]" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{suffix}"


def public_source(job: dict) -> bool:
    source = str(job.get("source", ""))
    return (
        "github.com/cli/cli/releases/download/" in source
        or "release-assets.githubusercontent.com/" in source
    )


def wait_job(db: str, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [job for job in jobs(db) if public_source(job)]
        if last:
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"public ordinary capture did not create a native job: {last}")


def wait_completed(db: str, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = next((job for job in jobs(db) if job.get("id") == job_id), None)
        if last and last.get("state") == "completed" and last.get("provisional") is False:
            return last
        if last and last.get("state") == "failed":
            raise RuntimeError(f"public native job failed: {json.dumps({k: last.get(k) for k in ('id', 'state', 'error', 'source')}, default=str)}")
        time.sleep(0.2)
    raise RuntimeError(f"public native job did not complete: {last}")


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
        if DOWNLOAD_MATCH in (
            str(item.get("filename", ""))
            + str(item.get("url", ""))
            + str(item.get("finalUrl", ""))
        )
    ]


def wait_browser_complete(port: int, timeout: float = 90.0) -> tuple[dict, Path]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = worker_downloads(port)
        except Exception:
            last = []
        complete = [item for item in matching_downloads(last) if item.get("state") == "complete" and not item.get("error")]
        if complete:
            path = Path(complete[-1]["filename"])
            if path.exists() and path.stat().st_size > 0:
                return complete[-1], path
        time.sleep(0.5)
    raise RuntimeError(f"public browser download did not complete: {last}")


def wait_no_browser_download(port: int, timeout: float = 8.0) -> list[dict]:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = worker_downloads(port)
        except Exception:
            last = []
        matches = matching_downloads(last)
        if any(item.get("state") in {"complete", "interrupted"} for item in matches):
            return matches
        time.sleep(0.25)
    return matching_downloads(last)


def click_link(client: public.cdp_drive.CDP, explicit: bool) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const old=document.querySelector('#dm-ordinary-redirect-link');old?.remove();"
        f"const a=document.createElement('a');a.id='dm-ordinary-redirect-link';a.href={json.dumps(DOWNLOAD_URL)};"
        + (f"a.download={json.dumps(DOWNLOAD_NAME)};" if explicit else "")
        + "a.textContent='Download public redirect';"
        "a.style.cssText='position:fixed;top:8px;left:8px;z-index:2147483647;padding:12px;background:#fff;color:#000';"
        "document.body.append(a);const r=a.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2,href:a.href,download:a.getAttribute('download')};})())"
    )
    point = json.loads(raw)
    expected = DOWNLOAD_NAME if explicit else None
    if point.get("download") != expected:
        raise RuntimeError(f"ordinary link metadata mismatch: {point}")
    for event_type in ("mousePressed", "mouseReleased"):
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": point["x"],
                "y": point["y"],
                "button": "left",
                "clickCount": 1,
                "modifiers": 0,
            },
        )
    return point


def commit_via_cli(home: str, inspector_port: int, job_id: str, destination: str) -> None:
    payload = {"id": job_id, "input": {"name": DOWNLOAD_NAME, "destination": destination}}
    result = subprocess.run(
        [BIN, "--commit", json.dumps(payload, separators=(",", ":"))],
        env=public.browser_env(home, inspector_port),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"--commit failed: exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}")


def safe_job(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "state": job.get("state"),
        "provisional": job.get("provisional"),
        "source": redacted_url(job.get("source")),
        "error": job.get("error"),
    }


def run_case(explicit: bool) -> dict:
    label = "prebrowser" if explicit else "observe-only"
    root = Path(tempfile.mkdtemp(prefix=f"dm-ordinary-redirect-{label}-"))
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
        db = db_path(str(home))
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
                PAGE,
            ],
            env=public.browser_env(str(home), inspector_port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, PAGE)
        diagnostic = public.extension_diagnostic(chrome_port)
        print(f"{label.upper()}-EXTENSION:", json.dumps(diagnostic, sort_keys=True), flush=True)
        point = click_link(client, explicit)
        print(f"{label.upper()}-TRUSTED-LINK:", json.dumps(point, sort_keys=True), flush=True)
        native_job = wait_job(db)
        print(f"{label.upper()}-NATIVE-JOB:", json.dumps(safe_job(native_job), sort_keys=True), flush=True)

        browser_record = None
        browser_size = None
        browser_hash = None
        if explicit:
            browser_matches = wait_no_browser_download(chrome_port)
            if browser_matches:
                raise RuntimeError(f"pre-browser path created browser records: {browser_matches}")
        else:
            browser_record, browser_path = wait_browser_complete(chrome_port)
            browser_bytes = browser_path.read_bytes()
            browser_size = len(browser_bytes)
            browser_hash = hashlib.sha256(browser_bytes).hexdigest()
            print(
                "OBSERVE-ONLY-BROWSER:",
                json.dumps(
                    {
                        "id": browser_record.get("id"),
                        "state": browser_record.get("state"),
                        "error": browser_record.get("error"),
                        "url": redacted_url(browser_record.get("url")),
                        "finalUrl": redacted_url(browser_record.get("finalUrl")),
                        "filename": browser_record.get("filename"),
                        "bytesReceived": browser_record.get("bytesReceived"),
                        "size": browser_size,
                        "sha256": browser_hash,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        destination = root / "Managed" / DOWNLOAD_NAME
        commit_via_cli(str(home), inspector_port, native_job["id"], str(destination))
        native_done = wait_completed(db, native_job["id"])
        native_size = destination.stat().st_size
        native_hash = hashlib.sha256(destination.read_bytes()).hexdigest()

        reference_size = None
        reference_hash = None
        if explicit:
            # This is a validation fetch, not a product consumer. It occurs
            # after the native result and is kept separate from the consumer
            # count so the one-owner assertion remains honest.
            with urllib.request.urlopen(DOWNLOAD_URL, timeout=60) as response:
                reference_bytes = response.read()
            reference_size = len(reference_bytes)
            reference_hash = hashlib.sha256(reference_bytes).hexdigest()
            if native_size != reference_size or native_hash != reference_hash:
                raise RuntimeError(
                    f"pre-browser native/reference mismatch: native={native_size}/{native_hash} reference={reference_size}/{reference_hash}"
                )
        else:
            if native_size != browser_size or native_hash != browser_hash:
                raise RuntimeError(
                    f"observe-only browser/native mismatch: browser={browser_size}/{browser_hash} native={native_size}/{native_hash}"
                )

        records = matching_downloads(worker_downloads(chrome_port))
        product_jobs = jobs(db)
        consumer_count = (1 if records else 0) + len(product_jobs)
        if explicit:
            assert not records, records
            assert len(product_jobs) == 1, product_jobs
            assert consumer_count == 1, consumer_count
        else:
            assert len(records) == 1, records
            assert len(product_jobs) == 1, product_jobs
            assert consumer_count == 2, consumer_count

        result = {
            "mode": label,
            "public_source": DOWNLOAD_URL,
            "native_job": safe_job(native_done),
            "native_bytes": native_size,
            "native_sha256": native_hash,
            "browser_records": len(records),
            "browser_download_id": records[0].get("id") if records else None,
            "browser_bytes": browser_size,
            "browser_sha256": browser_hash,
            "validation_bytes": reference_size,
            "validation_sha256": reference_hash,
            "product_consumer_count": consumer_count,
        }
        print(f"{label.upper()}-RESULT:", json.dumps(result, sort_keys=True), flush=True)
        return result
    except Exception:
        if app_log_path.exists():
            print(f"{label.upper()}-APP-LOG:", app_log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
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
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    observe_only = run_case(False)
    prebrowser = run_case(True)
    assert observe_only["product_consumer_count"] == 2, observe_only
    assert prebrowser["product_consumer_count"] == 1, prebrowser
    assert observe_only["native_sha256"] == observe_only["browser_sha256"], observe_only
    assert prebrowser["native_sha256"] == prebrowser["validation_sha256"], prebrowser
    print(
        "ORDINARY-REDIRECT-CHROMIUM: PASS "
        + json.dumps(
            {
                "public_source": DOWNLOAD_URL,
                "observe_only_consumers": observe_only["product_consumer_count"],
                "observe_only_browser_records": observe_only["browser_records"],
                "prebrowser_consumers": prebrowser["product_consumer_count"],
                "prebrowser_browser_records": prebrowser["browser_records"],
                "observe_only_sha256": observe_only["native_sha256"],
                "prebrowser_sha256": prebrowser["native_sha256"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    print("ORDINARY-REDIRECT-CHROMIUM-PROBE: PASS", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ORDINARY-REDIRECT-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
