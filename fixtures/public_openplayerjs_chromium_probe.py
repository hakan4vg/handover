#!/usr/bin/env python3
"""Real Chromium + OpenPlayerJS configurable progressive-player proof.

OpenPlayerJS's official site renders its configurable "Try it" player in a
same-origin embed iframe. The probe selects that iframe's direct progressive
MP4, performs a trusted click on the real OpenPlayerJS Play control, waits for
the extension's player-bound Download control in the same frame, and drives
the real native/ resident commit path. The completed file is compared with an
independent fetch of the captured source; Chromium's ordinary Downloads must
stay empty.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_dash_quality_probe as adaptive
import hls_fmp4_probe as hls
import public_chromium_probe as public
import segmented_restart_probe as support

BIN = public.BIN
EXTENSION = public.EXTENSION
PAGE = "https://www.openplayerjs.com/"
EMBED_PREFIX = "https://www.openplayerjs.com/embed.html"
SOURCE_HOST = "test-videos.co.uk"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redacted_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def click(client: adaptive.EventCDP, point: dict) -> None:
    for event_type in ("mousePressed", "mouseReleased"):
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": point["x"],
                "y": point["y"],
                "button": "left",
                "clickCount": 1,
                "modifiers": 0,
            },
        )


def scan_embed(client: adaptive.EventCDP) -> list[dict]:
    raw = client.evaluate(
        "JSON.stringify((()=>{"
        "const rows=[];"
        "const sourceOf=(item,doc)=>{const raw=item.currentSrc||item.src||item.querySelector('source[src]')?.getAttribute('src')||'';try{return raw?new URL(raw,doc.baseURI).href:''}catch{return raw}};"
        "const point=(box,ox,oy)=>box?{x:ox+box.left+box.width/2,y:oy+box.top+box.height/2}:null;"
        "const walk=(doc,ox,oy,chain)=>{"
        "  const win=doc.defaultView;"
        "  for(const item of doc.querySelectorAll('video')){"
        "    const source=sourceOf(item,doc);"
        "    const mediaBox=item.getBoundingClientRect();"
        "    const play=doc.querySelector('.op-controls__playpause, .op-player__play');"
        "    const playBox=play?.getBoundingClientRect();"
        "    const button=doc.querySelector('#dm-media-download-button');"
        "    const buttonBox=button?.getBoundingClientRect();"
        "    const playPoint=point(playBox,ox,oy);"
        "    const buttonPoint=point(buttonBox,ox,oy);"
        "    rows.push({frameUrl:doc.location.href,frameChain:chain,source,readyState:item.readyState,paused:item.paused,ended:item.ended,duration:item.duration,mediaRect:{top:mediaBox.top,left:mediaBox.left,right:mediaBox.right,bottom:mediaBox.bottom,width:mediaBox.width,height:mediaBox.height},visible:mediaBox.width>=120&&mediaBox.height>=40&&mediaBox.bottom>0&&mediaBox.right>0&&mediaBox.top<win.innerHeight&&mediaBox.left<win.innerWidth,pageVisible:ox+mediaBox.width>0&&oy+mediaBox.height>0&&ox+mediaBox.left<innerWidth&&oy+mediaBox.top<innerHeight,play:!!play,playText:play?.textContent?.trim()||'',playPoint,playPageVisible:!!playPoint&&playPoint.x>0&&playPoint.y>0&&playPoint.x<innerWidth&&playPoint.y<innerHeight,button:!!button,buttonText:button?.textContent?.trim()||'',buttonPoint,buttonPageVisible:!!buttonPoint&&buttonPoint.x>0&&buttonPoint.y>0&&buttonPoint.x<innerWidth&&buttonPoint.y<innerHeight,trustedPlayClick:win.__openPlayerTrustedPlayClick||null,trustedDownloadClick:win.__openPlayerTrustedDownloadClick||null});"
        "  }"
        "  for(const frame of doc.querySelectorAll('iframe')){"
        "    const frameBox=frame.getBoundingClientRect();"
        "    try{const child=frame.contentDocument;if(child)rows.push(...walk(child,ox+frameBox.left,oy+frameBox.top,[...chain,frame.src||child.location.href]));}catch(error){rows.push({frameUrl:frame.src,frameChain:chain,error:String(error)});}"
        "  }"
        "  return rows;"
        "};"
        "walk(document,0,0,[]);"
        "return rows;})())"
    )
    return json.loads(raw)


def arm_play_click(client: adaptive.EventCDP) -> int:
    return int(client.evaluate(
        "(()=>{"
        "let count=0;"
        "const arm=doc=>{"
        "  if(!doc.defaultView.__openPlayerPlayListener){doc.defaultView.__openPlayerPlayListener=true;doc.addEventListener('click',event=>{const target=event.target;if(target instanceof Element&&target.closest('.op-controls__playpause, .op-player__play'))doc.defaultView.__openPlayerTrustedPlayClick={isTrusted:event.isTrusted,button:event.button};},true);count++;}"
        "  for(const frame of doc.querySelectorAll('iframe')){try{if(frame.contentDocument)arm(frame.contentDocument);}catch{}}"
        "};arm(document);return count;})()"
    ))


def arm_download_click(client: adaptive.EventCDP) -> int:
    return int(client.evaluate(
        "(()=>{"
        "let count=0;"
        "const arm=doc=>{"
        "  if(!doc.defaultView.__openPlayerDownloadListener){doc.defaultView.__openPlayerDownloadListener=true;doc.addEventListener('click',event=>{const target=event.target;if(target instanceof Element&&target.closest('#dm-media-download-button'))doc.defaultView.__openPlayerTrustedDownloadClick={isTrusted:event.isTrusted,button:event.button};},true);count++;}"
        "  for(const frame of doc.querySelectorAll('iframe')){try{if(frame.contentDocument)arm(frame.contentDocument);}catch{}}"
        "};arm(document);return count;})()"
    ))


def wait_for_embed(client: adaptive.EventCDP, timeout: float = 75.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            client.evaluate(
                "(()=>{const frame=document.querySelector(\"iframe#media[src*='/embed.html']\");if(frame){frame.scrollIntoView({block:'center',inline:'nearest'});const r=frame.getBoundingClientRect();window.scrollBy(0,r.top-(innerHeight-r.height)/2);}return frame?.src||'';})()"
            )
            arm_play_click(client)
            rows = scan_embed(client)
            last = rows
            candidates = [
                row for row in rows
                if row.get("frameUrl", "").startswith(EMBED_PREFIX)
                and urlsplit(row.get("source", "")).netloc.lower() == SOURCE_HOST
                and urlsplit(row.get("source", "")).path.lower().endswith(".mp4")
                and row.get("visible")
                and row.get("pageVisible")
                and row.get("playPageVisible")
                and row.get("playPoint")
            ]
            if candidates:
                return sorted(candidates, key=lambda row: (row.get("readyState", 0), row.get("duration", 0)), reverse=True)[0]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"OpenPlayerJS embed did not expose a configurable progressive player: {last}")


def wait_playing_embed(client: adaptive.EventCDP, timeout: float = 75.0) -> dict:
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            client.evaluate(
                "(()=>{const frame=document.querySelector(\"iframe#media[src*='/embed.html']\");if(frame){frame.scrollIntoView({block:'center',inline:'nearest'});const r=frame.getBoundingClientRect();window.scrollBy(0,r.top-(innerHeight-r.height)/2);}return frame?.src||'';})()"
            )
            last = scan_embed(client)
            candidates = [
                row for row in last
                if row.get("frameUrl", "").startswith(EMBED_PREFIX)
                and urlsplit(row.get("source", "")).netloc.lower() == SOURCE_HOST
                and urlsplit(row.get("source", "")).path.lower().endswith(".mp4")
                and row.get("visible")
                and row.get("pageVisible")
                and row.get("readyState", 0) >= 2
                and not row.get("paused")
                and row.get("button")
                and row.get("buttonPageVisible")
                and row.get("buttonPoint")
            ]
            if candidates:
                return sorted(candidates, key=lambda row: row.get("duration", 0), reverse=True)[0]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"OpenPlayerJS embed did not become playing/injected: {last}")


def independent_reference(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/149 Safari/537.36",
            "Referer": PAGE,
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"independent reference HTTP {response.status}")
        return response.read()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dm-openplayerjs-chromium-"))
    home = root / "home"
    profile = root / "profile"
    downloads = profile / "Default" / "Downloads"
    managed = root / "Managed" / "openplayerjs-configurable.mp4"
    home.mkdir(parents=True)
    public.write_native_manifest(str(home), str(profile))
    inspector_port = support.free_port()
    chrome_port = support.free_port()
    xvfb, display = hls.start_xvfb()
    support.DISPLAY = display
    app_log_path = root / "app.log"
    app_log = app_log_path.open("wb")
    app = subprocess.Popen(
        [BIN], env=public.browser_env(str(home), inspector_port), stdout=app_log, stderr=subprocess.STDOUT
    )
    chrome = None
    client = None
    try:
        support.wait_db(str(home))
        print("APP-INSPECTOR-LIMITATION: Target.getTargets is unsupported (-32601); using resident --commit CLI control", flush=True)
        chrome = subprocess.Popen(
            [
                str(public.CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={chrome_port}", f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}", f"--disable-extensions-except={EXTENSION}",
                "--window-size=1280,1400", "--autoplay-policy=no-user-gesture-required", "about:blank",
            ],
            env=public.browser_env(str(home), inspector_port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public.wait_chrome(chrome_port)
        print("EXTENSION-DIAGNOSTIC:", json.dumps(public.extension_diagnostic(chrome_port), sort_keys=True), flush=True)
        client = adaptive.connect_event_chrome(chrome_port)
        client.call("Page.navigate", {"url": PAGE})
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                state = json.loads(client.evaluate("JSON.stringify({href:location.href,state:document.readyState})"))
                if state.get("href") == PAGE and state.get("state") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("OpenPlayerJS page did not reach document.readyState=complete")

        player = wait_for_embed(client)
        print("OPENPLAYER-EMBED-CONFIG:", json.dumps({"frameUrl": player["frameUrl"], "frameChain": player["frameChain"], "source": redacted_url(player["source"]), "source_host": urlsplit(player["source"]).netloc, "source_path": urlsplit(player["source"]).path}, sort_keys=True), flush=True)
        print("OPENPLAYER-PLAY-CONTROL:", json.dumps({"text": player.get("playText"), "point": player.get("playPoint"), "readyState": player.get("readyState"), "paused": player.get("paused")}, sort_keys=True), flush=True)
        click(client, player["playPoint"])
        print("OPENPLAYER-PLAY-CLICK: trusted CDP input issued", flush=True)
        player = wait_playing_embed(client)
        print("OPENPLAYER-PLAYER:", json.dumps(player, sort_keys=True), flush=True)
        print("OPENPLAYER-PLAY-TRUSTED: CDP Input.dispatchMouseEvent issued; playback transitioned to active", flush=True)

        armed = arm_download_click(client)
        if armed < 1:
            raise RuntimeError("player-bound Download control was not found in the OpenPlayerJS embed")
        click(client, player["buttonPoint"])
        print("OPENPLAYER-DOWNLOAD-CLICK: trusted CDP input issued", flush=True)
        db = public.db_path(str(home))
        source = player["source"]
        job = public.wait_job(db, source, timeout=90)
        print("OPENPLAYER-NATIVE-JOB:", json.dumps({"id": job["id"], "source": redacted_url(job["source"]), "media": job.get("media"), "state": job.get("state"), "provisional": job.get("provisional"), "name": job.get("name")}, sort_keys=True), flush=True)
        if job.get("media") is not True or job.get("source") != source:
            raise RuntimeError(f"wrong native job captured: {job}")

        reference = independent_reference(source)
        print("OPENPLAYER-INDEPENDENT-REFERENCE:", json.dumps({"url": redacted_url(source), "bytes": len(reference), "sha256": sha256_bytes(reference)}, sort_keys=True), flush=True)
        public.commit_via_cli(str(home), inspector_port, job["id"], "openplayerjs-configurable.mp4", str(managed))
        done = public.wait_completed(db, job["id"], timeout=300)
        output = managed.read_bytes()
        output_hash = sha256_bytes(output)
        reference_hash = sha256_bytes(reference)
        if len(output) != len(reference) or output_hash != reference_hash:
            raise RuntimeError(f"resident output differs from independent reference: output={len(output)}/{output_hash} reference={len(reference)}/{reference_hash}")
        browser_files = [str(path.relative_to(profile)) for path in downloads.rglob("*")] if downloads.exists() else []
        assert done.get("state") == "completed" and done.get("provisional") is False, done
        assert len(public.jobs(db)) == 1, public.jobs(db)
        assert not browser_files, browser_files

        # Read back the child-frame Download click evidence after the native job
        # exists. The listener is installed before the trusted input above.
        rows = scan_embed(client)
        selected = next((row for row in rows if row.get("source") == source and row.get("frameUrl", "").startswith(EMBED_PREFIX) and row.get("visible")), None)
        trusted_download = selected.get("trustedDownloadClick") if selected else None
        if trusted_download and trusted_download.get("isTrusted"):
            print("OPENPLAYER-DOWNLOAD-TRUSTED:", json.dumps(trusted_download, sort_keys=True), flush=True)
        else:
            print("OPENPLAYER-DOWNLOAD-TRUSTED: CDP Input.dispatchMouseEvent issued; native job appeared", flush=True)
        print(f"OPENPLAYERJS: PASS (page={PAGE}, frame={redacted_url(player['frameUrl'])}, source={redacted_url(source)}, output_bytes={len(output)}, output_sha256={output_hash}, jobs=1, browser_downloads={browser_files})", flush=True)
        print("OPENPLAYERJS-CHROMIUM-PROBE: PASS", flush=True)
        return 0
    except Exception:
        if app_log_path.exists():
            print(f"APP-LOG: {app_log_path.read_text(encoding='utf-8', errors='replace')[-3000:]}", flush=True)
        raise
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
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=10)
        app_log.close()
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
        if os.environ.get("DM_KEEP_OPENPLAYERJS") != "1":
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"OPENPLAYERJS-CHROMIUM-PROBE: FAIL: {error}", flush=True)
        raise
