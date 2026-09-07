#!/usr/bin/env python3
"""Real Chromium proof for manager inline controls and bulk actions in mock mode.

Covers SPEC §10.2 inline row pause/resume/retry, §10.3 Pause All/Resume All,
sorting, transient menu dismissal, and the Add URL overlay, all driven by trusted
CDP input against the explicit mock adapter.
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

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_chromium_probe as public

PAGE = "http://127.0.0.1:4173/"
ACTIVE_NAME = "ubuntu-24.04-desktop-amd64.iso"
FAILED_NAME = "old-archive.tar.xz"
LARGEST_NAME = "ubuntu-24.04-desktop-amd64.iso"


def evaluate_json(client, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def wait_page(client, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = evaluate_json(client, "{href:location.href,state:document.readyState,rows:document.querySelectorAll('.download-row').length}")
            if last.get("href") == PAGE and last.get("state") == "complete" and last.get("rows") == 14:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"mock manager did not load: {last}")


def click_point(client, point: dict) -> None:
    for event, button, buttons in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 0)):
        client.call("Input.dispatchMouseEvent", {"type": event, "x": point["x"], "y": point["y"], "button": button, "buttons": buttons, "clickCount": 1, "modifiers": 0})


def click_text(client, selector: str, text: str) -> dict:
    point = evaluate_json(
        client,
        "(()=>{const node=[...document.querySelectorAll(%s)].find(item=>{const text=item.textContent?.trim()||'';return text===%s||text.startsWith(%s);});"
        "if(!node)return {error:'missing'};const r=node.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,text:node.textContent.trim()};})()"
        % (json.dumps(selector), json.dumps(text), json.dumps(text)),
    )
    if point.get("error"):
        raise RuntimeError(f"missing {selector} text {text!r}")
    click_point(client, point)
    return point


def rows(client) -> list[dict]:
    return evaluate_json(
        client,
        "Array.from(document.querySelectorAll('.download-row')).map(row=>({"
        "name:row.querySelector('.row-title-line strong')?.textContent?.trim()||'',"
        "state:row.querySelector('.state')?.textContent?.trim()||'',"
        "action:row.querySelector('button[aria-label=\"Pause\"],button[aria-label=\"Resume\"],button[aria-label=\"Retry\"]')?.getAttribute('aria-label')||''"
        "}))",
    )


def row(client, name: str) -> dict:
    return next((item for item in rows(client) if item["name"] == name), {"error": name})


def row_action_point(client, name: str) -> dict:
    point = evaluate_json(
        client,
        "(()=>{const row=[...document.querySelectorAll('.download-row')].find(item=>item.querySelector('.row-title-line strong')?.textContent?.trim()==="
        + json.dumps(name)
        + ");const button=row?.querySelector('button[aria-label=\"Pause\"],button[aria-label=\"Resume\"],button[aria-label=\"Retry\"]');"
        "if(!button)return {error:'missing'};const r=button.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,label:button.getAttribute('aria-label')};})()",
    )
    if point.get("error"):
        raise RuntimeError(f"row action missing for {name}: {point}")
    return point


def wait_state(client, name: str, predicate: str, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = row(client, name)
        if eval(predicate, {}, {"last": last}):
            return last
        time.sleep(0.15)
    raise RuntimeError(f"{name} did not reach {predicate}: {last}")


def wait_toolbar(client, text: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    found: list[str] = []
    while time.time() < deadline:
        found = evaluate_json(client, "Array.from(document.querySelectorAll('.toolbar-actions button')).map(item=>item.textContent.trim())")
        if any(text in item for item in found):
            return
        time.sleep(0.15)
    raise RuntimeError(f"toolbar did not show {text}: {found}")


def click_sort_option(client, label: str) -> None:
    click_text(client, ".subtle-button", "Sort")
    time.sleep(0.3)
    click_text(client, ".sort-menu button", label)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-manager-inline-controls-"))
    profile = root / "profile"
    chrome_port = public.support.free_port()
    chrome = None
    client = None
    try:
        chrome = subprocess.Popen(
            [
                str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                "--window-size=1280,1400", PAGE,
            ],
            env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = public.connect_chrome(chrome_port)
        wait_page(client)
        print("INLINE-INITIAL:", json.dumps(rows(client), sort_keys=True), flush=True)

        # Inline Pause on the active row.
        point = row_action_point(client, ACTIVE_NAME)
        assert point["label"] == "Pause", point
        click_point(client, point)
        paused = wait_state(client, ACTIVE_NAME, "last['state'] == 'Paused' and last['action'] == 'Resume'")
        print("INLINE-PAUSE:", json.dumps(paused, sort_keys=True), flush=True)

        # Inline Resume.
        point = row_action_point(client, ACTIVE_NAME)
        assert point["label"] == "Resume", point
        click_point(client, point)
        resumed = wait_state(client, ACTIVE_NAME, "last['state'] not in ('Paused','Pending') and last['action'] == 'Pause'")
        print("INLINE-RESUME:", json.dumps(resumed, sort_keys=True), flush=True)

        # Inline Retry on the failed row.
        point = row_action_point(client, FAILED_NAME)
        assert point["label"] == "Retry", point
        click_point(client, point)
        retried = wait_state(client, FAILED_NAME, "last['state'] not in ('Failed',) and last['action'] == 'Pause'", timeout=10.0)
        print("INLINE-RETRY:", json.dumps(retried, sort_keys=True), flush=True)

        # Pause All.
        click_text(client, ".toolbar-actions button", "Pause All")
        all_paused = wait_state(client, ACTIVE_NAME, "last['state'] == 'Paused'", timeout=10.0)
        wait_toolbar(client, "Resume All")
        print("PAUSE-ALL:", json.dumps({"active": all_paused, "toolbar": evaluate_json(client, "Array.from(document.querySelectorAll('.toolbar-actions button')).map(item=>item.textContent.trim())")}, sort_keys=True), flush=True)

        # Resume All.
        click_text(client, ".toolbar-actions button", "Resume All")
        resumed_all = wait_state(client, ACTIVE_NAME, "last['state'] not in ('Paused','Pending') and last['action'] == 'Pause'", timeout=10.0)
        wait_toolbar(client, "Pause All")
        print("RESUME-ALL:", json.dumps({"active": resumed_all, "toolbar": evaluate_json(client, "Array.from(document.querySelectorAll('.toolbar-actions button')).map(item=>item.textContent.trim())")}, sort_keys=True), flush=True)

        # Selecting a row while the sort menu is open must dismiss the menu
        # rather than leaving an overlay stranded over the workspace.
        click_text(client, ".subtle-button", "Sort")
        time.sleep(0.25)
        assert evaluate_json(client, "!!document.querySelector('.sort-menu')")
        row_point = evaluate_json(client, "(()=>{const row=document.querySelector('.download-row');const r=row.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
        click_point(client, row_point)
        time.sleep(0.25)
        menu_after_row = evaluate_json(client, "!!document.querySelector('.sort-menu')")
        print("SORT-MENU-ROW-DISMISS:", json.dumps({"menu_after_row": menu_after_row}, sort_keys=True), flush=True)
        assert not menu_after_row, menu_after_row

        # Sort by Name: DOM order must match localeCompare order.
        click_sort_option(client, "Name")
        time.sleep(0.4)
        names = [item["name"] for item in rows(client)]
        expected = evaluate_json(client, "Array.from(document.querySelectorAll('.download-row')).map(row=>row.querySelector('.row-title-line strong')?.textContent?.trim()||'').sort((a,b)=>a.localeCompare(b))")
        assert names == expected, (names, expected)
        print("SORT-NAME:", json.dumps({"first": names[0], "count": len(names)}, sort_keys=True), flush=True)

        # Sort by File size: the largest job must come first.
        click_sort_option(client, "File size")
        time.sleep(0.4)
        names = [item["name"] for item in rows(client)]
        assert names[0] == LARGEST_NAME, names[:3]
        print("SORT-SIZE:", json.dumps({"first": names[0], "first_three": names[:3]}, sort_keys=True), flush=True)

        # Add URL opens the add-download overlay; Cancel closes it.
        click_text(client, ".toolbar-actions button", "Add URL")
        deadline = time.time() + 5
        overlay: dict = {}
        while time.time() < deadline:
            overlay = evaluate_json(client, "{open:!!document.querySelector('.add-download-window'),title:document.querySelector('.add-titlebar strong')?.textContent?.trim()||'',cancel:Array.from(document.querySelectorAll('.add-download-window button')).some(item=>item.textContent?.trim()==='Cancel')}")
            if overlay["open"]:
                break
            time.sleep(0.15)
        assert overlay["open"] and overlay["cancel"], overlay
        print("ADD-URL-OVERLAY:", json.dumps(overlay, sort_keys=True), flush=True)
        click_text(client, ".add-download-window button", "Cancel")
        deadline = time.time() + 5
        while time.time() < deadline:
            if not evaluate_json(client, "!!document.querySelector('.add-download-window')"):
                break
            time.sleep(0.15)
        assert not evaluate_json(client, "!!document.querySelector('.add-download-window')")
        print("ADD-URL-CLOSED: true", flush=True)

        print("MANAGER-INLINE-CONTROLS: PASS", flush=True)
        return 0
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
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MANAGER-INLINE-CONTROLS-PROBE: FAIL: {error}", flush=True)
        raise
