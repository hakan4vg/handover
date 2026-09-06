#!/usr/bin/env python3
"""Real native proof for General startup settings on the Linux test host.

The probe changes the two sign-in settings through the live Tauri inspector,
reads the disposable HOME's autostart file, and restarts the resident binary
with ``--startup`` while observing only X11 window visibility. It never touches
the real user's HOME or system autostart state.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")


def visible_main_windows(display: str) -> list[str]:
    result = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", "^Download Manager$"],
        env={**os.environ, "DISPLAY": display},
        capture_output=True,
        text=True,
        timeout=15,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def wait_visible(display: str, expected: bool, timeout: float = 15.0) -> list[str]:
    deadline = time.time() + timeout
    last: list[str] = []
    while time.time() < deadline:
        last = visible_main_windows(display)
        if bool(last) == expected:
            return last
        time.sleep(0.25)
    return last


def start_app(home: Path, display: str, startup: bool) -> tuple[subprocess.Popen[bytes], support.WebKitClient, BinaryIO]:
    inspector_port = support.free_port()
    log_path = home.parent / ("startup-app.log" if startup else "settings-app.log")
    log = log_path.open("wb")
    args = [BIN] + (["--startup"] if startup else [])
    app = subprocess.Popen(
        args,
        env={**support.app_env(str(home), inspector_port), "DISPLAY": display},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    db = support.wait_db(str(home))
    client = support.wait_inspector(inspector_port)
    support.wait_tauri(client)
    return app, client, log


def stop_app(app: subprocess.Popen[bytes] | None, client: support.WebKitClient | None, log: BinaryIO | None) -> None:
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    if app is not None:
        support.terminate_only(app, "startup settings app")
    if log is not None:
        log.close()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-startup-settings-"))
    home = root / "home"
    home.mkdir(parents=True)
    autostart = home / ".config" / "autostart" / "download-manager.desktop"
    xvfb = None
    app = None
    client = None
    log = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        app, client, log = start_app(home, display, startup=False)
        assert app.poll() is None, app.poll()
        initial_windows = wait_visible(display, True)
        assert initial_windows, initial_windows

        support.invoke(client, "update_settings", {"patch": {"startAtSignIn": False}})
        assert not autostart.exists(), autostart
        print("AUTOSTART-OFF:", json.dumps({"exists": autostart.exists()}, sort_keys=True), flush=True)

        support.invoke(client, "update_settings", {"patch": {"startAtSignIn": True}})
        assert autostart.exists(), autostart
        autostart_text = autostart.read_text(encoding="utf-8")
        assert "Type=Application" in autostart_text and "--startup" in autostart_text, autostart_text
        print("AUTOSTART-ON:", json.dumps({"exists": True, "has_startup_arg": "--startup" in autostart_text}, sort_keys=True), flush=True)

        support.invoke(client, "update_settings", {"patch": {"showManagerAtSignIn": False}})
        stop_app(app, client, log)
        app = client = log = None
        hidden_app, hidden_client, hidden_log = start_app(home, display, startup=True)
        app, client, log = hidden_app, hidden_client, hidden_log
        hidden_windows = wait_visible(display, False)
        assert not hidden_windows, hidden_windows
        print("STARTUP-HIDDEN:", json.dumps({"visible_main_windows": hidden_windows, "showManagerAtSignIn": False}, sort_keys=True), flush=True)
        stop_app(app, client, log)
        app = client = log = None

        app, client, log = start_app(home, display, startup=False)
        support.invoke(client, "update_settings", {"patch": {"showManagerAtSignIn": True}})
        stop_app(app, client, log)
        app = client = log = None
        shown_app, shown_client, shown_log = start_app(home, display, startup=True)
        app, client, log = shown_app, shown_client, shown_log
        shown_windows = wait_visible(display, True)
        assert len(shown_windows) == 1, shown_windows
        print("STARTUP-SHOWN:", json.dumps({"visible_main_windows": shown_windows, "showManagerAtSignIn": True}, sort_keys=True), flush=True)

        print("STARTUP-SETTINGS-PROBE: PASS", flush=True)
        return 0
    finally:
        stop_app(app, client, log)
        if xvfb is not None:
            support.terminate_only(xvfb, "startup settings Xvfb")
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STARTUP-SETTINGS-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
