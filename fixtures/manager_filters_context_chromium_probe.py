#!/usr/bin/env python3
"""Real Chromium proof for manager filters and row-context targeting in mock mode."""
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
EXPECTED = {
    "All": (14, {"ubuntu-24.04-desktop-amd64.iso", "Big Buck Bunny (1080p).mkv", "project-assets.zip", "Nature Documentary (4K).mkv", "Fedora-Workstation-Live-x86_64.iso", "old-archive.tar.xz", "Lecture 12 — Distributed Systems.mp4", "conference-keynote.webm", "design-system.pdf", "studio-recording.m4a", "City Walk (4K).mp4", "open-source-icons.zip", "product-tour.mp4", "backup-manifest.json"}),
    "Active": (3, {"ubuntu-24.04-desktop-amd64.iso", "Big Buck Bunny (1080p).mkv", "Fedora-Workstation-Live-x86_64.iso"}),
    "Paused": (1, {"project-assets.zip"}),
    "Completed": (8, {"Nature Documentary (4K).mkv", "Lecture 12 — Distributed Systems.mp4", "conference-keynote.webm", "design-system.pdf", "studio-recording.m4a", "City Walk (4K).mp4", "open-source-icons.zip", "product-tour.mp4"}),
    "Failed": (2, {"old-archive.tar.xz", "backup-manifest.json"}),
    "Media": (7, {"Big Buck Bunny (1080p).mkv", "Nature Documentary (4K).mkv", "Lecture 12 — Distributed Systems.mp4", "conference-keynote.webm", "studio-recording.m4a", "City Walk (4K).mp4", "product-tour.mp4"}),
}


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
    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"], "button": "none", "buttons": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "buttons": 1, "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "buttons": 0, "clickCount": 1, "modifiers": 0})


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
        "selected:row.classList.contains('selected')"
        "}))",
    )


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-manager-filters-"))
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

        keyboard = evaluate_json(
            client,
            "(()=>{const row=[...document.querySelectorAll('.download-row')].find(item=>item.querySelector('.row-title-line strong')?.textContent?.trim()==='project-assets.zip');"
            "if(!row)return {error:'keyboard target missing'};row.focus();row.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));"
            "return {role:row.getAttribute('role'),tabIndex:row.tabIndex,focused:document.activeElement===row};})()",
        )
        assert not keyboard.get("error"), keyboard
        assert keyboard["role"] == "button" and keyboard["tabIndex"] >= 0 and keyboard["focused"], keyboard
        deadline = time.time() + 5
        keyboard_heading = ""
        while time.time() < deadline:
            keyboard_heading = evaluate_json(client, "document.querySelector('.inspector-heading h2')?.textContent?.trim()||''")
            if keyboard_heading == "project-assets.zip":
                break
            time.sleep(0.1)
        assert keyboard_heading == "project-assets.zip", keyboard_heading
        click_text(client, ".download-row .row-title-line strong", "Big Buck Bunny")

        observed: dict[str, dict] = {}
        for label, (expected_count, expected_names) in EXPECTED.items():
            click_text(client, "button.sidebar-item", label)
            deadline = time.time() + 10
            current = []
            while time.time() < deadline:
                current = rows(client)
                if len(current) == expected_count:
                    break
                time.sleep(0.1)
            names = {item["name"] for item in current}
            assert len(current) == expected_count, (label, current)
            assert names == expected_names, (label, names, expected_names)
            navigation = evaluate_json(
                client,
                "(()=>{const items=[...document.querySelectorAll('button.sidebar-item')];return items.map(item=>({text:item.textContent?.trim()||'',selected:item.classList.contains('selected'),current:item.getAttribute('aria-current')}));})()",
            )
            selected_navigation = [item for item in navigation if item["selected"]]
            assert len(selected_navigation) == 1 and selected_navigation[0]["current"] == "page", (label, navigation)
            assert all(item["current"] is None for item in navigation if not item["selected"]), (label, navigation)
            heading = evaluate_json(client, "{title:document.querySelector('.toolbar-heading h1')?.textContent?.trim()||'',count:document.querySelector('.heading-count')?.textContent?.trim()||''}")
            assert heading["count"] == str(expected_count), (label, heading)
            selected_names = [item["name"] for item in current if item["selected"]]
            selected_heading = evaluate_json(client, "document.querySelector('.inspector-heading h2')?.textContent?.trim()||''")
            expected_selected = 1 if expected_count else 0
            assert len(selected_names) == expected_selected, (label, "selected rows", selected_names)
            if expected_count:
                assert selected_names[0] in expected_names, (label, selected_names, expected_names)
                assert selected_heading == selected_names[0], (label, selected_heading, selected_names)
            else:
                assert selected_heading == '', (label, selected_heading)
            observed[label] = {"count": len(current), "names": sorted(names), "heading": heading, "selected": selected_names, "inspector": selected_heading}

        click_text(client, "button.sidebar-item", "Settings")
        deadline = time.time() + 5
        settings_navigation = {}
        while time.time() < deadline:
            settings_navigation = evaluate_json(
                client,
                "(()=>{const items=[...document.querySelectorAll('button.settings-nav-item')];return items.map(item=>({text:item.textContent?.trim()||'',selected:item.classList.contains('selected'),current:item.getAttribute('aria-current')}));})()",
            )
            if settings_navigation:
                break
            time.sleep(0.1)
        selected_settings = [item for item in settings_navigation if item["selected"]]
        assert len(selected_settings) == 1 and selected_settings[0]["text"] == "General" and selected_settings[0]["current"] == "page", settings_navigation
        assert all(item["current"] is None for item in settings_navigation if not item["selected"]), settings_navigation
        click_text(client, "button.settings-nav-item", "Browser Integration")
        deadline = time.time() + 5
        while time.time() < deadline:
            settings_navigation = evaluate_json(
                client,
                "(()=>{const items=[...document.querySelectorAll('button.settings-nav-item')];return items.map(item=>({text:item.textContent?.trim()||'',selected:item.classList.contains('selected'),current:item.getAttribute('aria-current')}));})()",
            )
            selected_settings = [item for item in settings_navigation if item["selected"]]
            if selected_settings and selected_settings[0]["text"] == "Browser Integration":
                break
            time.sleep(0.1)
        assert len(selected_settings) == 1 and selected_settings[0]["current"] == "page", settings_navigation
        assert all(item["current"] is None for item in settings_navigation if not item["selected"]), settings_navigation
        click_text(client, "button.sidebar-item", "All")
        restored_selection = evaluate_json(client, "document.querySelector('.inspector-heading h2')?.textContent?.trim()||''")
        assert restored_selection == "Big Buck Bunny (1080p).mkv", restored_selection
        click_text(client, ".subtle-button", "Sort")
        sort_state = evaluate_json(
            client,
            "(()=>{const trigger=document.querySelector('.subtle-button');const menu=document.querySelector('.sort-menu');return {expanded:trigger?.getAttribute('aria-expanded'),role:menu?.getAttribute('role'),label:menu?.getAttribute('aria-label'),items:menu?.querySelectorAll('[role=\\\"menuitemradio\\\"]').length??0};})()",
        )
        assert sort_state == {"expanded": "true", "role": "menu", "label": "Sort downloads", "items": 4}, sort_state
        client.evaluate("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        deadline = time.time() + 5
        while time.time() < deadline and evaluate_json(client, "!!document.querySelector('.sort-menu')"):
            time.sleep(0.1)
        assert not evaluate_json(client, "!!document.querySelector('.sort-menu')"), "Sort menu stayed open after Escape"

        toolbar_more = evaluate_json(
            client,
            "(()=>{const trigger=document.querySelector('.toolbar-more');if(!trigger)return {error:'toolbar more missing'};const r=trigger.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()",
        )
        if toolbar_more.get("error"):
            raise RuntimeError(toolbar_more["error"])
        click_point(client, toolbar_more)
        toolbar_state = evaluate_json(
            client,
            "(()=>{const trigger=document.querySelector('.toolbar-more');const menu=document.querySelector('.toolbar-menu');return {expanded:trigger?.getAttribute('aria-expanded'),role:menu?.getAttribute('role'),label:menu?.getAttribute('aria-label'),items:menu?.querySelectorAll('[role=\\\"menuitem\\\"]').length??0};})()",
        )
        assert toolbar_state == {"expanded": "true", "role": "menu", "label": "Manager options", "items": 2}, toolbar_state
        client.evaluate("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        deadline = time.time() + 5
        while time.time() < deadline and evaluate_json(client, "!!document.querySelector('.toolbar-menu')"):
            time.sleep(0.1)
        assert not evaluate_json(client, "!!document.querySelector('.toolbar-menu')"), "Toolbar menu stayed open after Escape"

        target = evaluate_json(
            client,
            "(()=>{const row=[...document.querySelectorAll('.download-row')].find(item=>item.querySelector('.row-title-line strong')?.textContent?.trim()==='backup-manifest.json');"
            "const button=row?.querySelector('button[aria-label^=\"More actions for \"]');if(!row||!button)return {error:'target missing'};row.scrollIntoView({block:'center'});"
            "const r=button.getBoundingClientRect();return {x:r.left+3,y:r.top+3,row:row.querySelector('.row-title-line strong').textContent.trim()};})()",
        )
        if target.get("error"):
            raise RuntimeError(target["error"])
        click_point(client, target)
        menu = {"visible": False, "labels": []}
        deadline = time.time() + 5
        while time.time() < deadline:
            menu = evaluate_json(client, "{visible:!!document.querySelector('.context-menu'),labels:Array.from(document.querySelectorAll('.context-menu .menu-action')).map(item=>item.textContent.trim())}")
            if menu["visible"]:
                break
            time.sleep(0.1)
        assert menu["visible"] and "Remove from list" in menu["labels"], menu
        context_state = evaluate_json(
            client,
            "(()=>{const row=[...document.querySelectorAll('.download-row')].find(item=>item.querySelector('.row-title-line strong')?.textContent?.trim()==='backup-manifest.json');const trigger=[...row?.querySelectorAll('button')??[]].find(item=>(item.getAttribute('aria-label')||'').startsWith('More actions for '));const menu=document.querySelector('.context-menu');return {expanded:trigger?.getAttribute('aria-expanded'),role:menu?.getAttribute('role'),label:menu?.getAttribute('aria-label'),items:menu?.querySelectorAll('[role=\\\"menuitem\\\"]').length??0};})()",
        )
        assert context_state == {"expanded": "true", "role": "menu", "label": "Actions for backup-manifest.json", "items": 5}, context_state
        client.evaluate("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        deadline = time.time() + 5
        while time.time() < deadline and evaluate_json(client, "!!document.querySelector('.context-menu')"):
            time.sleep(0.1)
        assert not evaluate_json(client, "!!document.querySelector('.context-menu')"), "Context menu stayed open after Escape"
        click_point(client, target)
        deadline = time.time() + 5
        while time.time() < deadline:
            menu = evaluate_json(client, "{visible:!!document.querySelector('.context-menu'),labels:Array.from(document.querySelectorAll('.context-menu .menu-action')).map(item=>item.textContent.trim())}")
            if menu["visible"]:
                break
            time.sleep(0.1)
        assert menu["visible"] and "Remove from list" in menu["labels"], menu
        remove_point = evaluate_json(
            client,
            "(()=>{const item=[...document.querySelectorAll('.context-menu .menu-action')].find(node=>node.textContent.trim()==='Remove from list');item?.scrollIntoView({block:'center'});const r=item.getBoundingClientRect();return {x:r.left+3,y:r.top+3};})()",
        )
        click_point(client, remove_point)
        deadline = time.time() + 10
        after = []
        while time.time() < deadline:
            after = rows(client)
            if len(after) == 13 and all(item["name"] != "backup-manifest.json" for item in after):
                break
            time.sleep(0.1)
        assert len(after) == 13, after
        assert all(item["name"] != "backup-manifest.json" for item in after), after
        assert any(item["name"] == "Big Buck Bunny (1080p).mkv" and item["selected"] for item in after), after
        selected = evaluate_json(client, "document.querySelector('.inspector-heading h2')?.textContent?.trim()||''")
        assert selected == "Big Buck Bunny (1080p).mkv", selected
        print("FILTERS:", json.dumps(observed, sort_keys=True), flush=True)
        print("CONTEXT-TARGET:", json.dumps({"target": target, "menu": menu, "removed": "backup-manifest.json", "remaining": len(after), "selected": selected}, sort_keys=True), flush=True)
        print("MANAGER-FILTERS-CONTEXT-PROBE: PASS", flush=True)
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
        print(f"MANAGER-FILTERS-CONTEXT-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
