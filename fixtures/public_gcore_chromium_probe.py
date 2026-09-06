#!/usr/bin/env python3
"""Real Chromium/native proof for a public Gcore watch page.

Gcore's federated public watch page exposes a real HLS or progressive media
player. The probe reaches it with a trusted play click, clicks the real
Download Manager button, commits through the resident CLI, and compares the
managed output with an independent source reference.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://g-core.github.io/gcore-videoplayer-js/example/player.html"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def redact_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "[REDACTED]" if parsed.query else "", ""))
    except Exception:
        return re.sub(r"\?.*$", "?[REDACTED]", url)


def redact_text(text: str) -> str:
    return re.sub(r"https?://[^\s'\"]+", lambda match: redact_url(match.group(0)), text)


def evaluate_json(client, expression: str):
    raw = client.evaluate(expression)
    return json.loads(raw) if isinstance(raw, str) else raw


def click(client, point: dict) -> None:
    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "modifiers": 0})


def wait_page(client, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = evaluate_json(client, "JSON.stringify({href:location.href,state:document.readyState,title:document.title})")
            if last.get("href", "").split("#", 1)[0] == PAGE and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"GCORE page did not load: {last}")


def consent_point(client) -> dict | None:
    return evaluate_json(
        client,
        """(()=>{
          const candidates=[...document.querySelectorAll('button,[role="button"]')].filter(item=>{
            const text=(item.getAttribute('aria-label')||item.textContent||'').trim();
            const r=item.getBoundingClientRect();
            return /close this consent banner|close.*consent|reject|accept/i.test(text) && r.width>0 && r.height>0;
          });
          const item=candidates[0]; if(!item)return null;
          item.scrollIntoView({block:'start'});
          const r=item.getBoundingClientRect();
          return {x:r.left+r.width/2,y:r.top+r.height/2,text:(item.getAttribute('aria-label')||item.textContent||'').trim()};
        })()""",
    )


def consent_visible(client) -> bool:
    return bool(evaluate_json(client, """(()=>{const item=document.querySelector('.osano-cm-window,[aria-label=\"Cookie Consent Banner\"],.osano-cm-dialog');if(!item)return false;const r=item.getBoundingClientRect(),s=getComputedStyle(item);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'})()"""))


def action_point(client, action: str) -> dict | None:
    action_literal = json.dumps(action)
    return evaluate_json(client, f"""(()=>{{const item=[...document.querySelectorAll('#vod-panel [data-action]')].find(x=>x.dataset.action==={action_literal});if(!item)return null;item.scrollIntoView({{block:'center'}});const r=item.getBoundingClientRect();return r.width>20&&r.height>20&&r.bottom>0&&r.right>0?{{x:r.left+r.width/2,y:r.top+r.height/2,text:item.textContent.trim(),disabled:item.disabled}}:null}})()""")


def source_point(client, source_id: str = "spring") -> dict | None:
    source_literal = json.dumps(source_id)
    return evaluate_json(client, f"""(()=>{{const item=[...document.querySelectorAll('#vod-panel [data-source-id]')].find(x=>x.dataset.sourceId==={source_literal});if(!item)return null;item.scrollIntoView({{block:'center'}});const r=item.getBoundingClientRect();return r.width>20&&r.height>20&&r.bottom>0&&r.right>0?{{x:r.left+r.width/2,y:r.top+r.height/2,text:item.textContent.trim()}}:null}})()""")


def wait_point(client, getter, label: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = getter(client)
            if last:
                return last
        except Exception as error:
            last = {"error": str(error)[:160]}
        time.sleep(1.0)
    raise RuntimeError(f"GCORE {label} did not mount: {last}")


def pause_point(client) -> dict | None:
    point = action_point(client, "pause")
    return point if point and not point.get("disabled") else None


def play_point(client) -> dict | None:
    point = action_point(client, "play")
    return point if point and not point.get("disabled") else None


def wait_play_control(client, timeout: float = 120.0) -> dict:
    return wait_point(client, play_point, "VOD Play control", timeout)


def media_button_point(client) -> dict | None:
    return evaluate_json(client, """(()=>{const item=document.querySelector('#dm-media-download-button');if(!item)return null;const r=item.getBoundingClientRect();return r.width&&r.height?{x:r.left+r.width/2,y:r.top+r.height/2}:null})()""")


def gcore_state(client) -> dict:
    return evaluate_json(client, """(()=>{const v=document.querySelector('#vod-player video');const r=v?.getBoundingClientRect();return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,currentTime:v?.currentTime||0,duration:Number.isFinite(v?.duration)?v.duration:0,networkState:v?.networkState||0,error:v?.error?{code:v.error.code,message:v.error.message}:null,rect:r?{x:r.x,y:r.y,w:r.width,h:r.height}:null}})()""")


def wait_video_ready(client, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = gcore_state(client)
            if last.get("src") and last.get("readyState", 0) >= 2:
                return last
        except Exception as error:
            last = {"error": str(error)[:160]}
        time.sleep(1.0)
    raise RuntimeError(f"Gcore video did not become ready: {last}")


def wait_playable(client, timeout: float = 180.0) -> tuple[dict, dict]:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = gcore_state(client)
            point = media_button_point(client)
            if point and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("currentTime", 0) > 0:
                return last, point
        except Exception as error:
            last = {"error": str(error)[:160]}
        time.sleep(1.0)
    raise RuntimeError(f"GCORE player did not become playable/injected: {last}")


def wait_media_job(db: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last: list = []
    while time.time() < deadline:
        try:
            jobs = public.jobs(db)
            media = [job for job in jobs if job.get("media")]
            if media:
                return media[-1]
            last = [job.get("id") for job in jobs]
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"no GCORE media job appeared; jobs seen: {last}")


def fetch(url: str, timeout: int = 180, extra_headers: dict[str, str] | None = None) -> bytes:
    headers = {"User-Agent": UA, "Referer": PAGE}
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_range(url: str, start: int, length: int, timeout: int = 180) -> bytes:
    if start < 0 or length <= 0:
        raise ValueError(f"invalid HLS byte range start={start} length={length}")
    end = start + length - 1
    body = None
    status = None
    content_range = ""
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Referer": PAGE, "Range": f"bytes={start}-{end}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            content_range = response.headers.get("Content-Range", "")
            body = response.read()
    except Exception as error:
        raise RuntimeError(f"HLS byte-range request failed for {redact_url(url)} bytes={start}-{end}: {error}") from error
    if status != 206 or body is None or len(body) != length:
        raise RuntimeError(
            f"HLS byte-range response mismatch for {redact_url(url)} bytes={start}-{end}: "
            f"status={status} bytes={len(body) if body is not None else None} content_range={content_range!r}"
        )
    return body


def hls_attribute(line: str, name: str) -> str | None:
    match = re.search(rf"{re.escape(name)}=(?:\"([^\"]*)\"|([^,]*))", line)
    if not match:
        return None
    return (match.group(1) if match.group(1) is not None else match.group(2)).strip()


def parse_hls_range(value: str) -> tuple[int, int | None]:
    length_text, separator, offset_text = value.partition("@")
    length = int(length_text)
    offset = int(offset_text) if separator else None
    if length <= 0 or (offset is not None and offset < 0):
        raise ValueError(f"invalid HLS byte range {value!r}")
    return length, offset


def browser_source_reference(chrome_port: int, source: str) -> dict:
    session = public.connect_chrome(chrome_port)
    try:
        session.call("Network.enable")
        session.msg_id += 1
        navigate_id = session.msg_id
        public.cdp_drive.ws_send(session.sock, json.dumps({"id": navigate_id, "method": "Page.navigate", "params": {"url": source}}))
        request_id = None
        response = None
        deadline = time.time() + 60
        while time.time() < deadline:
            message = json.loads(public.cdp_drive.ws_recv(session.sock))
            if message.get("id") == navigate_id:
                continue
            method = message.get("method")
            params = message.get("params", {})
            if method == "Network.responseReceived":
                item = params.get("response", {})
                if item.get("url", "").split("#", 1)[0] == source.split("#", 1)[0]:
                    request_id = params.get("requestId")
                    response = {"status": item.get("status"), "mimeType": item.get("mimeType", "")}
            elif method == "Network.loadingFinished" and request_id and params.get("requestId") == request_id:
                break
        if not request_id or response is None:
            raise RuntimeError(f"browser CDP did not observe captured HLS response for {redact_url(source)}")
        body = session.call("Network.getResponseBody", {"requestId": request_id})
        raw = body.get("body", "")
        data = base64.b64decode(raw) if body.get("base64Encoded") else raw.encode("utf-8")
        if not data.startswith(b"#EXTM3U"):
            raise RuntimeError("browser CDP response was not an HLS manifest")
        return {"url": redact_url(source), "method": "cdp-network-response-body", "status": response["status"], "mimeType": response["mimeType"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "hls": True}
    finally:
        session.sock.close()


def hls_reference(source: str, root: Path) -> tuple[Path, dict]:
    manifest = fetch(source).decode("utf-8")
    if "#EXTM3U" not in manifest:
        raise RuntimeError("GCORE source was not an HLS manifest")
    selected = source
    lines = [line.strip() for line in manifest.splitlines() if line.strip()]
    variants: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            variants.append(urllib.parse.urljoin(source, lines[index + 1]))
    if variants:
        selected = variants[0]
    variant_body = fetch(selected).decode("utf-8")
    fragment_specs: list[tuple[str, tuple[int, int] | None]] = []
    pending_range: tuple[int, int | None] | None = None
    previous_url: str | None = None
    previous_end: int | None = None
    for line in variant_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MAP:"):
            uri = hls_attribute(stripped, "URI")
            if not uri:
                raise RuntimeError("GCORE HLS initialization map was missing URI")
            map_range = hls_attribute(stripped, "BYTERANGE")
            byte_range = None
            if map_range:
                length, offset = parse_hls_range(map_range)
                byte_range = (offset or 0, length)
            fragment_specs.insert(0, (urllib.parse.urljoin(selected, uri), byte_range))
        elif stripped.startswith("#EXT-X-BYTERANGE:"):
            pending_range = parse_hls_range(stripped.split(":", 1)[1].strip())
        elif stripped and not stripped.startswith("#"):
            url = urllib.parse.urljoin(selected, stripped)
            byte_range = None
            if pending_range is not None:
                length, offset = pending_range
                if offset is None:
                    if previous_url != url or previous_end is None:
                        raise RuntimeError("GCORE HLS byte range omitted its offset without a prior range on the same resource")
                    start = previous_end
                else:
                    start = offset
                byte_range = (start, length)
                previous_url, previous_end = url, start + length
            else:
                previous_url = None
                previous_end = None
            fragment_specs.append((url, byte_range))
            pending_range = None
    if not fragment_specs:
        raise RuntimeError("GCORE HLS variant contained no finite fragments")

    def fetch_spec(spec: tuple[str, tuple[int, int] | None]) -> bytes:
        url, byte_range = spec
        if byte_range is None:
            return fetch(url)
        return fetch_range(url, byte_range[0], byte_range[1])

    track_path = root / "gcore-reference.track"
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        fragment_bodies = list(pool.map(fetch_spec, fragment_specs))
    with track_path.open("wb") as handle:
        for body in fragment_bodies:
            handle.write(body)
    (root / "gcore-reference.m3u8").write_text(variant_body, encoding="utf-8")
    reference = root / "gcore-reference.mp4"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(track_path), "-map", "0", "-c", "copy", str(reference)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"GCORE reference ffmpeg failed: {result.stderr.strip()[-1000:]}")
    return reference, {
        "manifest_bytes": len(manifest.encode()),
        "variants": len(variants),
        "selected": redact_url(selected),
        "fragments": len(fragment_specs),
        "byte_ranges": sum(byte_range is not None for _, byte_range in fragment_specs),
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-gcore-chromium-", dir="/var/tmp"))
    home, profile = root / "home", root / "profile"
    downloads, managed_dir = profile / "Default" / "Downloads", root / "Managed"
    home.mkdir(parents=True); managed_dir.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port, chrome_port = support.free_port(), support.free_port()
    xvfb, display = hls.start_xvfb(); support.DISPLAY = display
    app_log_path = root / "app.log"; app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None; client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,600", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port); client = public.connect_chrome(chrome_port)
        client.call("Emulation.setDeviceMetricsOverride", {"width":1280,"height":600,"deviceScaleFactor":1,"mobile":False})
        client.call("Page.navigate", {"url": PAGE}); wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        consent = consent_point(client)
        if consent:
            click(client, consent); time.sleep(1.0)
            if consent_visible(client):
                client.evaluate("document.querySelector('.osano-cm-close,[aria-label=\"Close this consent banner\"]')?.click()")
                print("GCORE-CONSENT-FALLBACK: invoked the site's close handler after trusted close did not clear the banner", flush=True)
            deadline = time.time() + 10
            while consent_visible(client) and time.time() < deadline: time.sleep(0.5)
            if consent_visible(client): raise RuntimeError("GCORE consent banner remained after close")
        spring = wait_point(client, lambda current: source_point(current, "spring"), "Spring VOD source tab")
        click(client, spring)
        print("GCORE-SOURCE-CLICK: trusted CDP click selected Spring VOD", flush=True)
        state_before = wait_video_ready(client)
        print("GCORE-SOURCE-STATE:", json.dumps(state_before, sort_keys=True), flush=True)
        if state_before.get("readyState", 0) >= 2 and not state_before.get("paused", True):
            pause = wait_point(client, pause_point, "VOD Pause control")
            click(client, pause)
            deadline = time.time() + 20
            while time.time() < deadline:
                if gcore_state(client).get("paused", True):
                    break
                time.sleep(0.5)
            if not gcore_state(client).get("paused", True):
                raise RuntimeError("Gcore Pause gesture did not pause the VOD")
            print("GCORE-PAUSE-CLICK: trusted CDP click issued to expose VOD Play control", flush=True)
        point = wait_play_control(client)
        click(client, point); print("GCORE-PLAY-CLICK: trusted CDP click issued", flush=True)
        state, button = wait_playable(client)
        print("GCORE-PLAYER:", json.dumps(state, sort_keys=True), flush=True)
        print("GCORE-BUTTON:", json.dumps(button, sort_keys=True), flush=True)
        button_diagnostic = evaluate_json(client, """(()=>{const item=document.querySelector('#dm-media-download-button');if(!item)return {error:'button disappeared'};const r=item.getBoundingClientRect();window.__dmGcoreButtonEvent=null;item.addEventListener('click',event=>{window.__dmGcoreButtonEvent={isTrusted:event.isTrusted,defaultPrevented:event.defaultPrevented,target:event.target?.id||event.target?.tagName||null};},{capture:true});const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {outer:item.outerHTML,rect:{x:r.x,y:r.y,w:r.width,h:r.height},pointerEvents:getComputedStyle(item).pointerEvents,top:top?.outerHTML?.slice(0,500)||null};})()""")
        print("GCORE-BUTTON-DIAGNOSTIC:", json.dumps(button_diagnostic, sort_keys=True), flush=True)
        click(client, button)
        time.sleep(0.5)
        print("GCORE-BUTTON-EVENT:", json.dumps(evaluate_json(client, "JSON.stringify(window.__dmGcoreButtonEvent)"), sort_keys=True), flush=True)
        db = public.db_path(str(home)); created = wait_media_job(db)
        source = created.get("source") or ""
        if not source or source.startswith("blob:"): raise RuntimeError(f"GCORE native job did not expose a replayable source: {redact_url(source)}")
        print("GCORE-JOB:", json.dumps({"id":created.get("id"),"media":created.get("media"),"kind":created.get("kind"),"source":redact_url(source)}, sort_keys=True), flush=True)
        browser_reference = browser_source_reference(chrome_port, source)
        print("GCORE-BROWSER-SOURCE-REFERENCE:", json.dumps({**browser_reference,"url":redact_url(source)}, sort_keys=True), flush=True)
        assert browser_reference.get("hls") is True and browser_reference.get("bytes", 0) > 0, browser_reference
        reference, hls_meta = hls_reference(source, root); print("GCORE-HLS-REFERENCE:", json.dumps(hls_meta, sort_keys=True), flush=True)
        destination = managed_dir / "gcore-capture.mp4"; public.commit_via_cli(str(home), inspector_port, created["id"], destination.name, str(destination))
        completed = public.wait_completed(db, created["id"], timeout=600)
        output, reference_bytes = destination.read_bytes(), reference.read_bytes()
        output_hash, reference_hash = hashlib.sha256(output).hexdigest(), hashlib.sha256(reference_bytes).hexdigest()
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(output) == len(reference_bytes) and output_hash == reference_hash, {"output_bytes":len(output),"reference_bytes":len(reference_bytes),"output_sha256":output_hash,"reference_sha256":reference_hash}
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"GCORE: PASS (output_bytes={len(output)}, output_sha256={output_hash}, browser_source_bytes={browser_reference['bytes']}, browser_source_sha256={browser_reference['sha256']}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("PUBLIC-GCORE-CHROMIUM-PROBE: PASS", flush=True); return 0
    except Exception:
        if app_log_path.exists(): print("APP-LOG:", redact_text(app_log_path.read_text(encoding="utf-8", errors="replace")[-3000:]), flush=True)
        raise
    finally:
        if client is not None:
            try: client.sock.close()
            except Exception: pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try: chrome.wait(timeout=10)
            except subprocess.TimeoutExpired: chrome.kill(); chrome.wait(timeout=10)
        if app.poll() is None:
            app.terminate()
            try: app.wait(timeout=10)
            except subprocess.TimeoutExpired: app.kill(); app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try: xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired: xvfb.kill(); xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_PUBLIC_GCORE") != "1": shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-GCORE-CHROMIUM-PROBE: FAIL: {error}", flush=True); raise
