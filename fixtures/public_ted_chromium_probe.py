#!/usr/bin/env python3
"""Real Chromium/native proof for TED's media-controller HLS player.

TED's public talk page uses a media-controller custom element and feeds its
light-DOM <video> through a blob MediaSource from a finite HLS VOD manifest.
The probe closes only the site's consent banner, issues a trusted play click,
clicks the real Download Manager button, commits through the resident CLI,
and compares the committed MP4 with an independent FFmpeg reference built
from the exact HLS source captured from the browser path.
"""
from __future__ import annotations

import base64
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
PAGE = "https://www.ted.com/talks/chris_anderson_ted_s_secret_to_great_public_speaking"
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
    raise RuntimeError(f"TED page did not load: {last}")


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


def play_point(client) -> dict | None:
    return evaluate_json(
        client,
        """(()=>{
          const seen=new Set(),roots=[];
          const visit=root=>{if(!root||seen.has(root))return;seen.add(root);roots.push(root);for(const item of root.querySelectorAll?.('*')||[]){if(item.shadowRoot)visit(item.shadowRoot);}};
          visit(document); const candidates=[];
          for(const root of roots){for(const item of root.querySelectorAll?.('media-play-button,button,[role="button"]')||[]){
            const text=(item.getAttribute('aria-label')||item.getAttribute('title')||item.textContent||'').trim();const r=item.getBoundingClientRect();
            if(/^(play|play video|start|playback)$/i.test(text)||/^play/i.test(text)){if(r.width>20&&r.height>20&&r.bottom>0&&r.right>0)candidates.push({x:r.left+r.width/2,y:r.top+r.height/2,text});}
          }} return candidates[0]||null;
        })()""",
    )


def wait_play_control(client, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = play_point(client)
            if last:
                return last
        except Exception as error:
            last = {"error": str(error)[:160]}
        time.sleep(1.0)
    raise RuntimeError(f"TED play control did not mount: {last}")


def media_button_point(client) -> dict | None:
    return evaluate_json(client, """(()=>{const item=document.querySelector('#dm-media-download-button');if(!item)return null;const r=item.getBoundingClientRect();return r.width&&r.height?{x:r.left+r.width/2,y:r.top+r.height/2}:null})()""")


def ted_state(client) -> dict:
    return evaluate_json(client, """(()=>{const v=document.querySelector('media-controller video#video,video#video');const r=v?.getBoundingClientRect();return {src:v?.currentSrc||v?.src||'',readyState:v?.readyState||0,paused:v?.paused??true,currentTime:v?.currentTime||0,duration:Number.isFinite(v?.duration)?v.duration:0,networkState:v?.networkState||0,error:v?.error?{code:v.error.code,message:v.error.message}:null,rect:r?{x:r.x,y:r.y,w:r.width,h:r.height}:null}})()""")


def wait_playable(client, timeout: float = 180.0) -> tuple[dict, dict]:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = ted_state(client)
            point = media_button_point(client)
            if point and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("currentTime", 0) > 0:
                return last, point
        except Exception as error:
            last = {"error": str(error)[:160]}
        time.sleep(1.0)
    raise RuntimeError(f"TED player did not become playable/injected: {last}")


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
    raise RuntimeError(f"no TED media job appeared; jobs seen: {last}")


def fetch(url: str, timeout: int = 180) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": PAGE})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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
        raise RuntimeError("TED source was not an HLS manifest")
    selected = source
    lines = [line.strip() for line in manifest.splitlines() if line.strip()]
    variants: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            variants.append(urllib.parse.urljoin(source, lines[index + 1]))
    if variants:
        selected = variants[0]
    variant_body = fetch(selected).decode("utf-8")
    fragment_urls = [urllib.parse.urljoin(selected, line.strip()) for line in variant_body.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not fragment_urls:
        raise RuntimeError("TED HLS variant contained no finite fragments")
    (root / "ted-reference.m3u8").write_text(variant_body, encoding="utf-8")
    reference = root / "ted-reference.mp4"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-headers", f"Referer: {PAGE}\r\nUser-Agent: {UA}\r\n", "-i", selected, "-map", "0", "-c", "copy", str(reference)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"TED reference ffmpeg failed: {result.stderr.strip()[-1000:]}")
    return reference, {"manifest_bytes": len(manifest.encode()), "variants": len(variants), "selected": redact_url(selected), "fragments": len(fragment_urls)}


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-public-ted-chromium-"))
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
                print("TED-CONSENT-FALLBACK: invoked the site's close handler after trusted close did not clear the banner", flush=True)
            deadline = time.time() + 10
            while consent_visible(client) and time.time() < deadline: time.sleep(0.5)
            if consent_visible(client): raise RuntimeError("TED consent banner remained after close")
        point = wait_play_control(client)
        click(client, point); print("TED-PLAY-CLICK: trusted CDP click issued", flush=True)
        state, button = wait_playable(client)
        print("TED-PLAYER:", json.dumps(state, sort_keys=True), flush=True)
        print("TED-BUTTON:", json.dumps(button, sort_keys=True), flush=True)
        button_diagnostic = evaluate_json(client, """(()=>{const item=document.querySelector('#dm-media-download-button');if(!item)return {error:'button disappeared'};const r=item.getBoundingClientRect();window.__dmTedButtonEvent=null;item.addEventListener('click',event=>{window.__dmTedButtonEvent={isTrusted:event.isTrusted,defaultPrevented:event.defaultPrevented,target:event.target?.id||event.target?.tagName||null};},{capture:true});const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {outer:item.outerHTML,rect:{x:r.x,y:r.y,w:r.width,h:r.height},pointerEvents:getComputedStyle(item).pointerEvents,top:top?.outerHTML?.slice(0,500)||null};})()""")
        print("TED-BUTTON-DIAGNOSTIC:", json.dumps(button_diagnostic, sort_keys=True), flush=True)
        click(client, button)
        time.sleep(0.5)
        print("TED-BUTTON-EVENT:", json.dumps(evaluate_json(client, "JSON.stringify(window.__dmTedButtonEvent)"), sort_keys=True), flush=True)
        db = public.db_path(str(home)); created = wait_media_job(db)
        source = created.get("source") or ""
        if not source or source.startswith("blob:"): raise RuntimeError(f"TED native job did not expose a replayable source: {redact_url(source)}")
        print("TED-JOB:", json.dumps({"id":created.get("id"),"media":created.get("media"),"kind":created.get("kind"),"source":redact_url(source)}, sort_keys=True), flush=True)
        browser_reference = browser_source_reference(chrome_port, source)
        print("TED-BROWSER-SOURCE-REFERENCE:", json.dumps({**browser_reference,"url":redact_url(source)}, sort_keys=True), flush=True)
        assert browser_reference.get("hls") is True and browser_reference.get("bytes", 0) > 0, browser_reference
        reference, hls_meta = hls_reference(source, root); print("TED-HLS-REFERENCE:", json.dumps(hls_meta, sort_keys=True), flush=True)
        destination = managed_dir / "ted-talk.mp4"; public.commit_via_cli(str(home), inspector_port, created["id"], destination.name, str(destination))
        completed = public.wait_completed(db, created["id"], timeout=600)
        output, reference_bytes = destination.read_bytes(), reference.read_bytes()
        output_hash, reference_hash = hashlib.sha256(output).hexdigest(), hashlib.sha256(reference_bytes).hexdigest()
        assert completed.get("state") == "completed" and completed.get("provisional") is False, completed
        assert len(output) == len(reference_bytes) and output_hash == reference_hash, {"output_bytes":len(output),"reference_bytes":len(reference_bytes),"output_sha256":output_hash,"reference_sha256":reference_hash}
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print(f"TED: PASS (output_bytes={len(output)}, output_sha256={output_hash}, browser_source_bytes={browser_reference['bytes']}, browser_source_sha256={browser_reference['sha256']}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("PUBLIC-TED-CHROMIUM-PROBE: PASS", flush=True); return 0
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
        if os.environ.get("DM_KEEP_PUBLIC_TED") != "1": shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC-TED-CHROMIUM-PROBE: FAIL: {error}", flush=True); raise
