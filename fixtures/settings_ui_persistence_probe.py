#!/usr/bin/env python3
"""Real resident-app proof that Appearance settings persist across restart."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN = str(Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "download-manager")
THEME = "dark"
ACCENT = "#d3138c"


def wait_dom(client, expression: str, predicate, timeout: float = 30.0):
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
    raise RuntimeError(f"DOM condition did not become true: {expression}; last={last!r}")


def click_text(client, selector: str, text: str) -> None:
    expression = (
        "(()=>{const node=[...document.querySelectorAll(%s)].find(item=>item.textContent?.trim()===%s);"
        "if(!node)return 'missing';node.click();return 'clicked';})()"
        % (json.dumps(selector), json.dumps(text))
    )
    result = client.evaluate(expression)
    if result != "clicked":
        raise RuntimeError(f"could not click {selector} text {text!r}: {result!r}")


def open_settings_appearance(client) -> None:
    click_text(client, "button.sidebar-item", "Settings")
    wait_dom(client, "document.querySelector('.settings-main')?.innerText||''", lambda value: "General" in (value or ""))
    click_text(client, "button.settings-nav-item", "Appearance")
    wait_dom(client, "document.querySelector('.settings-heading h1')?.textContent||''", lambda value: value == "Appearance")


def rendered_state(client) -> dict:
    raw = client.evaluate(
        "JSON.stringify((()=>{const shell=document.querySelector('.desktop-shell');"
        "return {theme:shell?.dataset.theme||'',density:shell?.dataset.density||'',"
        "style:shell?.getAttribute('style')||'',accentSelected:!!document.querySelector('button.accent-swatch.selected[aria-label=\"Use #d3138c accent\"]'),"
        "densityValue:document.querySelector('.settings-content select')?.value||''};})())"
    )
    return json.loads(raw)


def boot(home: Path, inspector_port: int):
    app_log = (home.parent / f"app-{time.time_ns()}.log").open("wb")
    app = subprocess.Popen([BIN], env=support.app_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    db = support.wait_db(str(home))
    client = support.wait_inspector(inspector_port)
    support.wait_tauri(client)
    return app, db, client, app_log


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-settings-ui-"))
    home = root / "home"
    home.mkdir(parents=True)
    inspector_port = support.free_port()
    app = None
    client = None
    app_log = None
    xvfb = None
    try:
        xvfb, display = hls.start_xvfb()
        support.DISPLAY = display
        app, db, client, app_log = boot(home, inspector_port)
        assert app.poll() is None, app.poll()
        open_settings_appearance(client)
        initial = rendered_state(client)
        print("SETTINGS-UI-INITIAL:", json.dumps(initial, sort_keys=True), flush=True)

        click_text(client, "button.theme-option", "Dark")
        click_text(client, f"button.accent-swatch[aria-label='Use {ACCENT} accent']", "")
        wait_dom(
            client,
            "JSON.stringify((()=>{const shell=document.querySelector('.desktop-shell');return {theme:shell?.dataset.theme||'',density:shell?.dataset.density||'',style:shell?.getAttribute('style')||'',selected:!!document.querySelector('button.accent-swatch.selected[aria-label=\"Use #d3138c accent\"]'),select:document.querySelector('.settings-content select')?.value||''}})())",
            lambda value: (lambda state: state.get("theme") == THEME and state.get("selected"))(json.loads(value)),
            timeout=30.0,
        )
        saved = support.load_settings(db)
        assert saved.get("theme") == THEME, saved
        assert saved.get("accent") == ACCENT, saved
        changed = rendered_state(client)
        print("SETTINGS-UI-CHANGED:", json.dumps(changed, sort_keys=True), flush=True)
        print("SETTINGS-DB-SAVED:", json.dumps({"theme": saved.get("theme"), "accent": saved.get("accent")}, sort_keys=True), flush=True)

        client.close()
        client = None
        support.terminate_only(app, "settings persistence app")
        app = None
        app_log.close()
        app_log = None
        time.sleep(0.5)

        app, db, client, app_log = boot(home, inspector_port)
        open_settings_appearance(client)
        restored = wait_dom(
            client,
            "JSON.stringify((()=>{const shell=document.querySelector('.desktop-shell');return {theme:shell?.dataset.theme||'',density:shell?.dataset.density||'',style:shell?.getAttribute('style')||'',selected:!!document.querySelector('button.accent-swatch.selected[aria-label=\"Use #d3138c accent\"]'),select:document.querySelector('.settings-content select')?.value||''}})())",
            lambda value: (lambda state: state.get("theme") == THEME and state.get("selected"))(json.loads(value)),
            timeout=30.0,
        )
        persisted = support.load_settings(db)
        assert persisted.get("theme") == THEME, persisted
        assert persisted.get("accent") == ACCENT, persisted
        print("SETTINGS-UI-RESTORED:", restored, flush=True)
        print(f"SETTINGS-UI-PERSISTENCE: PASS (theme={THEME}, accent={ACCENT}, sqlite_match=true, restart=true)", flush=True)
        print("SETTINGS-UI-PROBE: PASS", flush=True)
        return 0
    finally:
        if client is not None:
            client.close()
        if app is not None:
            support.terminate_only(app, "settings persistence app")
        if app_log is not None:
            app_log.close()
        if xvfb is not None:
            support.terminate_only(xvfb, "settings persistence Xvfb")
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SETTINGS-UI-PROBE: FAIL: {error}", file=sys.stderr, flush=True)
        raise
