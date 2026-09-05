#!/usr/bin/env python3
"""Real native/UI proof for completion notifications and the Notifications surface."""
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
FILENAME = "browser-fallback.bin"


def invoke(client: support.WebKitClient, command: str, payload: dict | None = None):
    expression = (
        "(async()=>await window.__TAURI_INTERNALS__.invoke("
        f"{json.dumps(command)}, {json.dumps(payload or {})}))()"
    )
    return client.evaluate(expression)


def snapshot(client: support.WebKitClient) -> dict:
    raw = client.evaluate("(async()=>JSON.stringify(await window.__TAURI_INTERNALS__.invoke('get_snapshot')))()")
    return json.loads(raw)


def wait_job(db: str, source: str, predicate, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        last = [job for job in public.jobs(db) if job.get("source") == source]
        if last and predicate(last[-1]):
            return last[-1]
        time.sleep(0.2)
    raise RuntimeError(f"notification probe job did not reach expected state: {last}")


def wait_notification(client: support.WebKitClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = snapshot(client)
        match = next((item for item in last.get("notifications", []) if item.get("jobId") == job_id and item.get("type") == "completed"), None)
        if match:
            return match
        time.sleep(0.2)
    raise RuntimeError(f"completion notification did not appear: {last.get('notifications')}")


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
            last = json.loads(raw)
            if last.get("surface") and last.get("cards", 0) >= 1:
                return last
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Notifications surface did not render: {last}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-notification-surface-"))
    home = root / "home"
    managed_dir = root / "Managed"
    home.mkdir(parents=True)
    server = fallback.DownloadServer()
    source = f"http://127.0.0.1:{server.port}/{FILENAME}"
    inspector_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    env = support.app_env(str(home), inspector_port)
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=env, stdout=app_log, stderr=subprocess.STDOUT)
    client = None
    try:
        support.wait_db(str(home))
        client = support.wait_inspector(inspector_port, timeout=30)
        support.wait_tauri(client)
        db = public.db_path(str(home))
        raw = json.dumps({"type": "capture-acquisition", "payload": {"source": source, "name": "notification.bin"}}, separators=(",", ":"))
        forwarded = subprocess.run([BIN, "--capture", raw], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if forwarded.returncode != 0:
            raise RuntimeError(f"capture forwarding failed: {forwarded.returncode}")
        provisional = wait_job(db, source, lambda job: job.get("provisional") is True and job.get("state") in {"finalizing", "downloading", "connecting"})
        print("NATIVE-JOB:", json.dumps({"id": provisional["id"], "source": provisional["source"], "state": provisional["state"], "provisional": provisional.get("provisional")}, sort_keys=True), flush=True)
        managed_name = "notification-surface.bin"
        managed = managed_dir / managed_name
        public.commit_via_cli(str(home), inspector_port, provisional["id"], managed_name, str(managed))
        completed = wait_job(db, source, lambda job: job.get("state") == "completed" and job.get("provisional") is False, timeout=180)
        subprocess.run([BIN], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        client.evaluate("(window.location.href=window.location.pathname+'?view=notifications','navigated')")
        surface = wait_surface(client)
        print("NOTIFICATIONS-SURFACE:", json.dumps(surface, sort_keys=True), flush=True)
        output_size = managed.stat().st_size
        output_hash = hashlib.sha256(managed.read_bytes()).hexdigest()
        expected_hash = hashlib.sha256(fallback.PAYLOAD).hexdigest()
        assert output_size == len(fallback.PAYLOAD), output_size
        assert output_hash == expected_hash, (output_hash, expected_hash)
        assert surface["cards"] == 1, surface
        assert surface["buttons"] == ["Open", "Show in folder"], surface
        assert "Download completed" in surface["text"], surface
        assert "notification-surface.bin" in surface["text"], surface
        assert server.requests == 1, server.requests
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        print(f"NOTIFICATION-SURFACE-PROBE: PASS (job={provisional['id']}, output_bytes={output_size}, output_sha256={output_hash}, notification_type=completed, cards={surface['cards']}, buttons={surface['buttons']}, source_requests={server.requests})", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
        raise
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass
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
        if os.environ.get("DM_KEEP_NOTIFICATION") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"NOTIFICATION-SURFACE-PROBE: FAIL: {error}", flush=True)
        raise
