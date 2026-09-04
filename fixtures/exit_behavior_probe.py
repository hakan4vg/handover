#!/usr/bin/env python3
"""Real proof that closeBehavior=exit terminates the resident application."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
import segmented_restart_probe as support
import hls_fmp4_probe as hls

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")


def native_manager_state(home: str) -> tuple[str, str]:
    tree = subprocess.check_output(["xwininfo", "-root", "-tree"], env=support.app_env(home), text=True)
    match = re.search(r'^\s*(0x[0-9a-f]+) "Download Manager":', tree, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Download Manager window not found in X tree: {tree}")
    info = subprocess.check_output(["xwininfo", "-id", match.group(1)], env=support.app_env(home), text=True)
    map_match = re.search(r'Map State:\s*(\S+)', info)
    return match.group(1), map_match.group(1).strip() if map_match else f"unknown:{info}"


def main() -> int:
    home = tempfile.mkdtemp(prefix="dm-exit-behavior-")
    xvfb = None
    resident = None
    client = None
    log_handle = None
    try:
        settings_db = Path(support.db_path(home))
        settings_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings_db) as connection:
            connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("INSERT INTO settings (id, payload) VALUES (1, ?)", (json.dumps({"closeBehavior": "exit"}),))
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        time.sleep(1.0)
        inspector_port = support.free_port()
        environment = support.app_env(home, inspector_port)
        log_handle = open(os.path.join(home, "resident.log"), "wb")
        resident = subprocess.Popen([BIN], env=environment, stdout=log_handle, stderr=subprocess.STDOUT)
        try:
            client = support.wait_inspector(inspector_port, timeout=30)
        except Exception as error:
            log_handle.flush()
            log = Path(home, "resident.log").read_text(errors="replace")
            raise RuntimeError(f"Inspector startup failed: app_poll={resident.poll()} display=:99 port={inspector_port} log={log!r}") from error
        support.wait_tauri(client)
        result = client.evaluate("(document.querySelector('button[aria-label=\\\"Settings\\\"]')?.click(), 'clicked')")
        assert result == "clicked", result
        select_ready = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            select_ready = client.evaluate("Array.from(document.querySelectorAll('select')).some(item=>Array.from(item.options).some(option=>option.value==='exit')) ? 'ready' : 'missing'")
            if select_ready == "ready":
                break
            time.sleep(0.2)
        assert select_ready == "ready", select_ready
        result = client.evaluate("(()=>{const select=Array.from(document.querySelectorAll('select')).find(item=>Array.from(item.options).some(option=>option.value==='exit')); const setter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set; setter.call(select,'exit'); select.dispatchEvent(new Event('input',{bubbles:true})); select.dispatchEvent(new Event('change',{bubbles:true})); return select.value;})()")
        assert result == "exit", result
        live_behavior = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            live_behavior = client.evaluate("Array.from(document.querySelectorAll('select')).find(select => Array.from(select.options).some(option => option.value === 'exit'))?.value ?? ''")
            if live_behavior == "exit":
                break
            time.sleep(0.2)
        assert live_behavior == "exit", live_behavior
        window_id, map_state = native_manager_state(home)
        assert map_state == "IsViewable", map_state
        close = subprocess.run(["xdotool", "windowclose", window_id], env=support.app_env(home), capture_output=True, text=True)
        assert close.returncode == 0, close.stderr
        client.close()
        client = None
        deadline = time.time() + 10
        while time.time() < deadline and resident.poll() is None:
            time.sleep(0.1)
        exit_code = resident.poll()
        assert exit_code is not None, exit_code
        log = Path(home, "resident.log").read_text(errors="replace")
        assert exit_code != -11, log
        print(f"EXIT-BEHAVIOR: PASS (closeBehavior=exit, resident_exit={exit_code}, native_close=WM_DELETE_WINDOW)", flush=True)
        print("EXIT-BEHAVIOR-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if resident is not None:
            support.terminate_only(resident, "exit-behavior resident process")
        if log_handle is not None:
            log_handle.close()
        if xvfb is not None:
            support.terminate_only(xvfb, "exit-behavior Xvfb")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"EXIT-BEHAVIOR-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
