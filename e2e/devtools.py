"""Calls the resident's commands the way its own UI does: through the main
window's webview, reached over the DevTools protocol (e2e/cdp.mjs)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PORT = 38219
CDP = Path(__file__).with_name("cdp.mjs")


def environment() -> dict[str, str]:
    """Environment for the resident's process that opens the DevTools port.
    DM_TEST_INSTANCE keeps its windows invisible and out of the user's focus."""
    return {**os.environ, "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={PORT}", "DM_TEST_INSTANCE": "1"}


MANAGER = r"^http://tauri\.localhost/$"


def evaluate(expression: str, timeout: float = 60, page: str = MANAGER):
    """Evaluate an expression in a window (the manager by default); return its JSON value."""
    done = subprocess.run(["node", str(CDP), str(PORT), page, expression], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or "DevTools evaluation failed")
    return json.loads(done.stdout)


def invoke(command: str, args: dict | None = None, timeout: float = 60):
    return evaluate(f"window.__TAURI_INTERNALS__.invoke({json.dumps(command)}, {json.dumps(args or {})})", timeout)
