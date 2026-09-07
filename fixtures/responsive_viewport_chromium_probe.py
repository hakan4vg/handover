#!/usr/bin/env python3
"""Real Chromium proof that the manager stays contained at a narrow viewport.

The probe uses the running Vite mock surface and a fresh Chromium profile. CDP
sets deterministic 900x600, 841x560, and 780x560 CSS viewports because headless
Chromium may ignore its launch window-size flag. The manager must not make the
document taller or wider than the viewport, its status footer must remain inside
the window, and the two-column inspector must not be clipped at the transition
width.
"""
from __future__ import annotations

import base64
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
import segmented_restart_probe as support

URL = "http://127.0.0.1:4177/"
VIEWPORTS = (
    ("desktop", {"width": 900, "height": 600, "deviceScaleFactor": 1, "mobile": False}, Path("/tmp/dm-ui-responsive-chromium.png")),
    ("transition", {"width": 841, "height": 560, "deviceScaleFactor": 1, "mobile": False}, Path("/tmp/dm-ui-responsive-transition.png")),
    ("minimum", {"width": 780, "height": 560, "deviceScaleFactor": 1, "mobile": False}, Path("/tmp/dm-ui-responsive-min.png")),
)


def evaluate_json(client, expression: str):
    return json.loads(client.evaluate(f"JSON.stringify({expression})"))


def wait_for_render(client, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = evaluate_json(client, "{state:document.readyState,text:document.body.innerText.slice(0,120)}")
            if last.get("state") == "complete" and "Download Manager" in last.get("text", "") and "Active" in last.get("text", ""):
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"mock manager did not render: {last}")


def measure(client) -> dict:
    return evaluate_json(client, """(()=>{
      const q=(selector)=>document.querySelector(selector);
      const box=(selector)=>{const element=q(selector), rect=element?.getBoundingClientRect(), style=element&&getComputedStyle(element); return element&&rect?{top:rect.top,bottom:rect.bottom,left:rect.left,right:rect.right,height:rect.height,width:rect.width,minHeight:style.minHeight,overflow:style.overflow}:null;};
      return {
        viewport:{width:innerWidth,height:innerHeight},
        document:{clientWidth:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,clientHeight:document.documentElement.clientHeight,scrollHeight:document.documentElement.scrollHeight},
        body:{clientWidth:document.body.clientWidth,scrollWidth:document.body.scrollWidth,clientHeight:document.body.clientHeight,scrollHeight:document.body.scrollHeight},
        shell:box('.desktop-shell'), manager:box('.manager-body'), footer:box('.manager-statusbar'),
        workspace:box('.workspace-columns'), list:box('.download-list'), inspector:box('.inspector'),
      };
    })()""")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-ui-responsive-chromium-"))
    profile = root / "profile"
    port = support.free_port()
    chrome = subprocess.Popen(
        [
            str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
            "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
            f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--window-size=900,600", URL,
        ],
        env={**os.environ, "HOME": str(root / "home")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = None
    try:
        public.wait_chrome(port)
        client = public.connect_chrome(port)
        for label, viewport, screenshot_path in VIEWPORTS:
            client.call("Emulation.setDeviceMetricsOverride", viewport)
            client.call("Page.reload", {"ignoreCache": True})
            wait_for_render(client)
            metrics = measure(client)
            print(f"RESPONSIVE-VIEWPORT[{label}]:", json.dumps(metrics, sort_keys=True), flush=True)
            screenshot = client.call("Page.captureScreenshot", {"format": "png"})
            screenshot_path.write_bytes(base64.b64decode(screenshot["data"]))
            print(f"RESPONSIVE-VIEWPORT-SCREENSHOT[{label}]: {screenshot_path} bytes={screenshot_path.stat().st_size}", flush=True)
            document = metrics["document"]
            body = metrics["body"]
            footer = metrics["footer"]
            manager = metrics["manager"]
            workspace = metrics["workspace"]
            inspector = metrics["inspector"]
            viewport_size = metrics["viewport"]
            assert document["scrollHeight"] == document["clientHeight"], metrics
            assert document["scrollWidth"] == document["clientWidth"], metrics
            assert body["scrollHeight"] == body["clientHeight"], metrics
            assert body["scrollWidth"] == body["clientWidth"], metrics
            assert manager["minHeight"] == "0px", metrics
            assert footer["bottom"] <= viewport_size["height"], metrics
            if viewport_size["width"] > 840:
                assert workspace["right"] <= manager["right"] + 1, metrics
                assert inspector["width"] > 0, metrics
                assert inspector["right"] <= workspace["right"] + 1, metrics
            print(f"RESPONSIVE-VIEWPORT: PASS ({label} contained)", flush=True)
        return 0
    finally:
        if client is not None:
            try:
                client.sock.close()
            except Exception:
                pass
        if chrome.poll() is None:
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
        print(f"RESPONSIVE-VIEWPORT-PROBE: FAIL: {error}", flush=True)
        raise
