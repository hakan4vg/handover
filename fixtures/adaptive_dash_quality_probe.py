#!/usr/bin/env python3
"""Real Chromium adaptive DASH representation-selection proof.

The official DASH-IF reference player is driven through its visible MPD field.
The probe uses dash.js's documented manual representation API, records exact
video/audio request URLs from CDP Network events, captures through the real
extension/native host, and checks the completed native MP4 against the browser's
selected representation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hls_fmp4_probe as hls
import public_chromium_probe as public
import public_dash_chromium_probe as public_dash
import segmented_restart_probe as support
import cdp_drive

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://reference.dashif.org/dash.js/latest/samples/dash-if-reference-player/index.html"
MPD_SOURCE = "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps_shortened.mpd"
TARGET_VIDEO_ID = "bbb_30fps_320x180_200k"
TARGET_VIDEO_PREFIX = f"https://dash.akamaized.net/akamai/bbb_30fps/{TARGET_VIDEO_ID}/"
AUDIO_PREFIX = "https://dash.akamaized.net/akamai/bbb_30fps/bbb_a64k/"


class EventCDP(cdp_drive.CDP):
    """CDP client that retains asynchronous Network events while waiting."""

    def __init__(self, sock):
        super().__init__(sock)
        self.events: list[dict] = []

    def call(self, method, params=None, timeout=60):
        self.msg_id += 1
        cdp_drive.ws_send(
            self.sock,
            json.dumps({"id": self.msg_id, "method": method, "params": params or {}}),
        )
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise RuntimeError(f"timeout waiting for {method}")
            reply = json.loads(cdp_drive.ws_recv(self.sock))
            if reply.get("id") == self.msg_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            self.events.append(reply)

    def take_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events


def connect_event_chrome(port: int) -> EventCDP:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        targets = json.load(response)
    page = next((item for item in targets if item.get("type") == "page"), None)
    if page is None:
        raise RuntimeError(f"Chromium has no page target: {targets}")
    ws_url = page["webSocketDebuggerUrl"]
    path = ws_url.split(f"127.0.0.1:{port}", 1)[1]
    client = EventCDP(cdp_drive.ws_connect(port, path))
    client.call("Runtime.enable")
    client.call("Page.enable")
    client.call("Network.enable", {"maxTotalBufferSize": 20 * 1024 * 1024, "maxResourceBufferSize": 2 * 1024 * 1024})
    client.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1400, "deviceScaleFactor": 1, "mobile": False})
    client.take_events()
    return client


def wait_page(client: EventCDP, timeout: float = 60.0) -> None:
    client.call("Page.navigate", {"url": PAGE})
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
            if last.get("href", "").split("#", 1)[0] == PAGE and last.get("state") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"public DASH page did not load: {last}")


def load_public_mpd(client: EventCDP, timeout: float = 90.0) -> dict:
    client.evaluate(
        "(()=>{"
        f"const input=document.querySelector('#stream-url');input.value={json.dumps(MPD_SOURCE)};"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        "document.querySelector('#btn-load').click();return input.value;"
        "})()"
    )
    deadline = time.time() + timeout
    last = None
    last_error = None
    while time.time() < deadline:
        try:
            last = json.loads(
                client.evaluate(
                    "(async()=>{"
                    "const v=document.querySelector('video');"
                    "if(v){v.scrollIntoView({block:'center'});v.muted=true;try{await v.play()}catch{}}"
                    "const p=window.player;"
                    "const current=p?.getCurrentRepresentationForType?.('video');"
                    "const reps=p?.getRepresentationsByType?.('video')||[];"
                    "const r=v?.getBoundingClientRect();"
                    "return JSON.stringify({src:v?.currentSrc||v?.src||'',blob:(v?.currentSrc||v?.src||'').startsWith('blob:'),readyState:v?.readyState||0,paused:v?.paused??true,duration:v?.duration??0,mpd:document.querySelector('#stream-url')?.value||'',button:!!document.querySelector('#dm-media-download-button'),player:!!p,reps:reps.map(x=>({id:x.id,bandwidth:x.bandwidth,width:x.width,height:x.height})),current:current?{id:current.id,bandwidth:current.bandwidth,width:current.width,height:current.height}:null,rect:r?{top:r.top,left:r.left,right:r.right,bottom:r.bottom,width:r.width,height:r.height}:null});"
                    "})()"
                )
            )
            if last.get("blob") and last.get("readyState", 0) >= 2 and not last.get("paused") and last.get("button") and last.get("player") and len(last.get("reps", [])) >= 2 and last.get("mpd") == MPD_SOURCE:
                return last
        except Exception as error:
            last_error = repr(error)
        time.sleep(0.5)
    raise RuntimeError(f"public DASH MSE player did not become playable/injected: {last}; last_error={last_error}")


def force_representation(client: EventCDP) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const targetId={json.dumps(TARGET_VIDEO_ID)};"
        "const p=window.player;"
        "if(!p)return JSON.stringify({error:'window.player missing'});"
        "const reps=p.getRepresentationsByType('video');"
        "const before=p.getCurrentRepresentationForType('video');"
        "const target=reps.find(x=>x.id===targetId);"
        "if(!target)return JSON.stringify({error:'target representation missing',reps:reps.map(x=>({id:x.id,width:x.width,height:x.height,bandwidth:x.bandwidth}))});"
        "p.updateSettings({streaming:{abr:{autoSwitchBitrate:{video:false}}}});"
        "p.setRepresentationForTypeById('video',target.id,true);"
        "const after=p.getCurrentRepresentationForType('video');"
        "return JSON.stringify({before:before?{id:before.id,width:before.width,height:before.height,bandwidth:before.bandwidth}:null,target:{id:target.id,width:target.width,height:target.height,bandwidth:target.bandwidth},after:after?{id:after.id,width:after.width,height:after.height,bandwidth:after.bandwidth}:null});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(result)
    if result.get("target", {}).get("id") != TARGET_VIDEO_ID:
        raise RuntimeError(f"DASH representation API did not target requested ID: {result}")
    return result


def collect_requests(client: EventCDP, events: list[dict]) -> list[dict]:
    records = []
    seen = set()
    for event in events + client.take_events():
        if event.get("method") != "Network.requestWillBeSent":
            continue
        params = event.get("params", {})
        request = params.get("request", {})
        url = request.get("url", "")
        if not (url.endswith(".m4v") or url.endswith(".m4a")):
            continue
        key = (url, params.get("requestId"))
        if key in seen:
            continue
        seen.add(key)
        records.append({"url": url, "requestId": params.get("requestId"), "type": request.get("initialPriority"), "timestamp": params.get("timestamp")})
    return records


def wait_selected_representation(client: EventCDP, change: dict, timeout: float = 60.0) -> tuple[dict, list[dict]]:
    deadline = time.time() + timeout
    last = None
    observed: list[dict] = []
    while time.time() < deadline:
        try:
            last = json.loads(
                client.evaluate(
                    "JSON.stringify((()=>{"
                    "const p=window.player;const c=p?.getCurrentRepresentationForType?.('video');const v=document.querySelector('video');"
                    "return {current:c?{id:c.id,width:c.width,height:c.height,bandwidth:c.bandwidth}:null,playing:!(v?.paused??true),resources:performance.getEntriesByType('resource').map(e=>e.name).filter(n=>/\\.(m4v|m4a)(?:[?#]|$)/i.test(n)).slice(-40)};"
                    "})())"
                )
            )
        except Exception:
            last = None
        observed.extend(collect_requests(client, []))
        urls = [item["url"] for item in observed]
        if (
            last
            and last.get("current", {}).get("id") == change["target"]["id"]
            and last.get("playing")
            and any(url.startswith(TARGET_VIDEO_PREFIX) for url in urls)
            and any(url.startswith(AUDIO_PREFIX) for url in urls)
        ):
            return last, observed
        time.sleep(0.5)
    raise RuntimeError(f"selected DASH representation requests were not observed: state={last}, requests={observed}")


def browser_track_reference(client: EventCDP, urls: list[str]) -> dict:
    raw = client.evaluate(
        "(async()=>{"
        f"const urls={json.dumps(urls)};"
        "const parts=[];"
        "for(const url of urls){const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status+' '+url);parts.push(new Uint8Array(await response.arrayBuffer()));}"
        "const joined=new Uint8Array(parts.reduce((sum,part)=>sum+part.length,0));let offset=0;for(const part of parts){joined.set(part,offset);offset+=part.length}"
        "const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',joined));"
        "return JSON.stringify({requests:urls,segments:urls.length,size:joined.length,hash:Array.from(digest).map(x=>x.toString(16).padStart(2,'0')).join('')});"
        "})()"
    )
    result = json.loads(raw)
    if result.get("error"):
        raise RuntimeError(f"browser selected-track reference failed: {result}")
    return result


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-adaptive-dash-quality-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen([BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT)
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}", f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}", "--window-size=1280,1000", "--autoplay-policy=no-user-gesture-required", "about:blank"],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        client = connect_event_chrome(chrome_port)
        wait_page(client)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        player = load_public_mpd(client)
        print("ADAPTIVE-DASH-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        client.take_events()
        change = force_representation(client)
        print("ADAPTIVE-DASH-REPRESENTATION-CHANGE:", json.dumps(change, sort_keys=True), flush=True)
        selected_state, traffic = wait_selected_representation(client, change)
        video_requests = []
        audio_requests = []
        for item in traffic:
            url = item["url"]
            if url.startswith(TARGET_VIDEO_PREFIX) and url not in video_requests:
                video_requests.append(url)
            if url.startswith(AUDIO_PREFIX) and url not in audio_requests:
                audio_requests.append(url)
        print("ADAPTIVE-DASH-SELECTED-STATE:", json.dumps(selected_state, sort_keys=True), flush=True)
        print("ADAPTIVE-DASH-BROWSER-TRAFFIC:", json.dumps({"video": video_requests, "audio": audio_requests}, sort_keys=True), flush=True)
        assert video_requests and audio_requests, traffic

        button = json.loads(client.evaluate("JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');const r=b?.getBoundingClientRect();return r?{x:r.left+r.width/2,y:r.top+r.height/2}:null})())"))
        if not button:
            raise RuntimeError("media button disappeared after DASH representation change")
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": button["x"], "y": button["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": button["x"], "y": button["y"], "button": "left", "clickCount": 1, "modifiers": 0})
        db = public.db_path(str(home))
        job = public.wait_job(db, MPD_SOURCE, timeout=90)
        print("ADAPTIVE-DASH-JOB:", json.dumps({"id": job["id"], "source": job["source"], "state": job["state"]}, sort_keys=True), flush=True)
        destination = root / "Downloads" / "adaptive-dash-selected.mp4"
        public.commit_via_cli(str(home), inspector_port, job["id"], "adaptive-dash-selected.mp4", str(destination))
        video_reference = browser_track_reference(client, video_requests)
        audio_reference = browser_track_reference(client, audio_requests)
        print("BROWSER-SELECTED-VIDEO-REFERENCE:", json.dumps(video_reference, sort_keys=True), flush=True)
        print("BROWSER-SELECTED-AUDIO-REFERENCE:", json.dumps(audio_reference, sort_keys=True), flush=True)
        completed = public.wait_completed(db, job["id"], timeout=180)
        output_size = destination.stat().st_size
        output_hash = support.sha256(str(destination))
        ffprobe_run = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
        ffprobe = json.loads(ffprobe_run.stdout)
        video_streams = [stream for stream in ffprobe.get("streams", []) if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in ffprobe.get("streams", []) if stream.get("codec_type") == "audio"]
        result = {"completed": completed, "output_bytes": output_size, "output_sha256": output_hash, "ffprobe": ffprobe, "selected_video": video_reference, "selected_audio": audio_reference, "traffic": {"video": video_requests, "audio": audio_requests}}
        print("ADAPTIVE-DASH-NATIVE-RESULT:", json.dumps(result, sort_keys=True), flush=True)
        assert completed.get("state") == "completed" and completed.get("provisional") is False, result
        assert video_streams and audio_streams, result
        assert video_streams[0].get("width") == 320 and video_streams[0].get("height") == 180, result
        ownership = public_dash.browser_ownership(client)
        assert ownership["context"] and not ownership["context"]["defaultPrevented"] and ownership["context"]["isTrusted"], ownership
        assert ownership["click"] and not ownership["click"]["defaultPrevented"] and ownership["click"]["isTrusted"] and ownership["click"]["ctrlKey"], ownership
        assert len(public.jobs(db)) == 1, public.jobs(db)
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert not browser_files, browser_files
        print("BROWSER-OWNERSHIP: PASS", json.dumps(ownership, sort_keys=True), flush=True)
        print(f"ADAPTIVE-DASH-QUALITY: PASS (selected={TARGET_VIDEO_ID}, video_requests={len(video_requests)}, audio_requests={len(audio_requests)}, output_bytes={output_size}, output_sha256={output_hash}, browser_downloads={browser_files})", flush=True)
        print("ADAPTIVE-DASH-QUALITY-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')}", flush=True)
        raise
    finally:
        if client is not None:
            try:
                client.sock.close()
            except Exception:
                pass
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
        if os.environ.get("DM_KEEP_ADAPTIVE_DASH") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ADAPTIVE-DASH-QUALITY-PROBE: FAIL: {error}", flush=True)
        raise
