#!/usr/bin/env python3
"""Real resident-app proof of the manual Add URL -> Download flow.

The main WebKit surface is inspector-addressable even though captured browser
windows are separate targets. This probe submits a local URL through the actual
Add URL overlay and activates its real Download button. It never calls
create_provisional or commit_provisional directly; those commands are reached
through the rendered UI callbacks.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
FILENAME = "manual-ui.bin"
PAYLOAD = bytes((index * 37 + 19) % 256 for index in range(256 * 1024))


class DownloadServer:
    def __init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/manual-ui.bin":
                    self.send_error(404)
                    return
                with owner.lock:
                    owner.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

        self.lock = threading.Lock()
        self.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def wait_until(client, expression: str, predicate, timeout: float = 30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = client.evaluate(expression)
            if predicate(last):
                return last
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"condition did not become true: {expression}; last={last!r}")


def set_input(client, index: int, value: str) -> None:
    expression = (
        "(()=>{const input=document.querySelectorAll('.add-download-window input')[%d];"
        "if(!input)return false;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        "setter.call(input,%s);input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));return input.value;})()"
        % (index, json.dumps(value))
    )
    result = client.evaluate(expression)
    if result != value:
        raise RuntimeError(f"input {index} did not accept value: {result!r}")


def buttons(client) -> list[dict]:
    result = client.evaluate(
        "JSON.stringify([...document.querySelectorAll('.add-download-window button')].map(button=>({text:button.textContent?.trim()||'',className:button.className,disabled:button.disabled})))"
    )
    return json.loads(result)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-manual-add-ui-"))
    home = root / "home"
    home.mkdir(parents=True)
    server = DownloadServer()
    xvfb = None
    app = None
    client = None
    app_log = None
    source = f"http://127.0.0.1:{server.port}/{FILENAME}"
    destination = home / "Downloads" / FILENAME
    inspector_port = support.free_port()
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        app_log = (root / "app.log").open("wb")
        app = subprocess.Popen(
            [BIN],
            env=support.app_env(str(home), inspector_port),
            stdout=app_log,
            stderr=subprocess.STDOUT,
        )
        db = support.wait_db(str(home))
        client = support.wait_inspector(inspector_port)
        support.wait_tauri(client)
        assert app.poll() is None, app.poll()

        initial = client.evaluate("document.body.innerText") or ""
        assert "Add URL" in initial, initial[:500]
        assert server.requests == 0, server.requests
        client.evaluate("[...document.querySelectorAll('button')].find(button=>button.textContent?.includes('Add URL'))?.click()")
        wait_until(client, "Boolean(document.querySelector('.add-download-window.manual'))", bool)
        print("MANUAL-ADD-OVERLAY:", json.dumps(buttons(client), sort_keys=True), flush=True)

        set_input(client, 0, source)
        set_input(client, 1, FILENAME)
        assert server.requests == 0, server.requests
        manual_buttons = buttons(client)
        assert any(item.get("text") == "Start Download" and not item.get("disabled") for item in manual_buttons), manual_buttons
        submit_result = client.evaluate(
            "(()=>{const button=document.querySelector('.add-download-window.manual button.button.primary');"
            "if(!button||button.disabled)return 'not-ready';button.click();return button.textContent?.trim()||'';})()"
        )
        assert submit_result == "Start Download", submit_result
        assert server.requests == 0, server.requests
        wait_until(
            client,
            "Boolean(document.querySelector('.add-download-window.captured'))",
            bool,
            timeout=30.0,
        )
        deadline = time.time() + 60.0
        job = None
        while time.time() < deadline:
            jobs = [item for item in support.read_jobs(db) if item.get("source") == source]
            if jobs:
                job = jobs[0]
                if job.get("provisional") is True and job.get("state") in {"finalizing", "completed", "failed"}:
                    break
            time.sleep(0.1)
        if job is None:
            raise RuntimeError(f"manual provisional job did not appear: {support.read_jobs(db)}")
        assert job.get("provisional") is True, job
        assert server.requests == 1, server.requests
        print("MANUAL-ADD-PROVISIONAL:", json.dumps({"id": job.get("id"), "state": job.get("state"), "provisional": job.get("provisional"), "source": job.get("source")}, sort_keys=True), flush=True)

        set_input(client, 2, str(destination))
        click_result = client.evaluate(
            "[...document.querySelectorAll('.add-download-window.captured button')].find(button=>button.textContent?.trim()==='Download')?.click();'clicked'"
        )
        assert click_result == "clicked", click_result
        job_id = job["id"]
        completed = support.wait_job(db, job_id, lambda row: row.get("state") == "completed", timeout=60.0)
        assert completed.get("provisional") is False, completed
        assert completed.get("destination") == str(destination), completed
        assert destination.exists(), destination
        assert destination.read_bytes() == PAYLOAD
        output_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        assert server.requests == 1, server.requests
        wait_until(client, "!document.querySelector('.add-download-window')", bool, timeout=20.0)
        print(
            f"MANUAL-ADD-UI: PASS (job={job_id}, requests={server.requests}, state={completed['state']}, provisional={completed['provisional']}, bytes={destination.stat().st_size}, sha256={output_hash}, destination={destination})",
            flush=True,
        )
        print("MANUAL-ADD-UI-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "manual Add URL app")
        if app_log is not None:
            app_log.close()
        server.stop()
        if xvfb is not None:
            support.terminate_only(xvfb, "manual Add URL Xvfb")
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MANUAL-ADD-UI-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
