#!/usr/bin/env python3
"""Fresh Chromium proof for extension popup actions in explicit mock mode."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

import public_chromium_probe as public

PAGE = "http://127.0.0.1:4173/?view=extension&site=example.com"


def evaluate_json(client, expression: str):
    value = client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
    result = value.get("result", {})
    if "exceptionDetails" in value:
        raise RuntimeError(value["exceptionDetails"])
    return result.get("value")


def wait_dom(client, expression: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = evaluate_json(client, expression)
        if last:
            return last
        time.sleep(0.15)
    raise RuntimeError(f"DOM wait timed out: {last}")


def click_label(client, label: str):
    point = wait_dom(
        client,
        "(()=>{const n=[...document.querySelectorAll('button')].find(item=>item.textContent?.trim()==="
        + json.dumps(label)
        + ");if(!n)return null;const r=n.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()",
    )
    print("POPUP-CLICK-POINT:", json.dumps({"label": label, "point": point, "hit": evaluate_json(client, f"(()=>{{const e=document.elementFromPoint({point['x']},{point['y']});return {{tag:e?.tagName,text:e?.textContent?.trim()}};}})()")}), file=sys.stderr, flush=True)
    for event, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call(
            "Input.dispatchMouseEvent",
            {"type": event, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1},
        )


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-popup-"))
    profile = root / "profile"
    profile.mkdir()
    chrome = None
    client = None
    try:
        chrome_port = public.support.free_port()
        chrome = subprocess.Popen(
            [
                public.CHROME,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={chrome_port}",
                f"--user-data-dir={profile}",
                "--window-size=1280,1400",
                PAGE,
            ],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port, timeout=30)
        client = public.connect_chrome(chrome_port)
        public.wait_page(client, PAGE, timeout=30)
        initial = wait_dom(client, "document.querySelector('.popup-surface') && document.body.innerText")
        print("POPUP-INITIAL:", type(initial).__name__, repr(initial), file=sys.stderr, flush=True)
        containment = evaluate_json(client, "({viewportHeight:innerHeight,documentHeight:document.documentElement.clientHeight,documentScrollHeight:document.documentElement.scrollHeight,bodyHeight:document.body.clientHeight,bodyScrollHeight:document.body.scrollHeight})")
        print("POPUP-CONTAINMENT:", json.dumps(containment, sort_keys=True), file=sys.stderr, flush=True)
        assert containment["documentScrollHeight"] == containment["documentHeight"], containment
        assert containment["bodyScrollHeight"] == containment["bodyHeight"], containment
        assert "CURRENT SITE" in initial and "example.com" in initial, initial
        assert "Exclude this site" in initial, initial

        click_label(client, "Exclude this site")
        time.sleep(0.5)
        excluded = evaluate_json(client, "({text:document.body.innerText,settings:JSON.parse(localStorage.getItem('download-manager.settings'))})")
        assert "Enable on this site" in excluded["text"], excluded
        settings_after_exclude = excluded["settings"]
        assert "example.com" in settings_after_exclude["excludedSites"], settings_after_exclude

        click_label(client, "Enable on this site")
        time.sleep(0.5)
        enabled = evaluate_json(client, "({text:document.body.innerText,settings:JSON.parse(localStorage.getItem('download-manager.settings'))})")
        assert "Exclude this site" in enabled["text"], enabled
        settings_after_enable = enabled["settings"]
        assert "example.com" not in settings_after_enable["excludedSites"], settings_after_enable

        click_label(client, "Open Manager")
        wait_dom(client, "location.search==='' && !!document.querySelector('.manager-main')", timeout=15)
        manager = evaluate_json(
            client,
            "({href:location.href,heading:document.querySelector('.toolbar-heading')?.innerText||'',rows:document.querySelectorAll('.download-row').length})",
        )
        assert manager["rows"] == 14 and manager["heading"].startswith("All"), manager
        print(
            "EXTENSION-POPUP-PROBE: PASS "
            + json.dumps(
                {
                    "excluded_site": "example.com",
                    "excluded_then_enabled": True,
                    "manager_rows": manager["rows"],
                    "manager_heading": manager["heading"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        print(f"EXTENSION-POPUP-PROBE: FAIL: {type(exc).__name__}: {exc!r}", file=sys.stderr, flush=True)
        return 1
    finally:
        if client is not None:
            try:
                client.sock.close()
            except Exception:
                pass
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
