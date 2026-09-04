#!/usr/bin/env python3
"""Cold-start real Chromium capture proof."""
from __future__ import annotations
import json, os, shutil, signal, sqlite3, subprocess, tempfile, time
from pathlib import Path
import public_chromium_probe as public
import public_hls_chromium_probe as public_hls
import hls_fmp4_probe as hls
import segmented_restart_probe as support

BIN=public.BIN
EXTENSION=public.EXTENSION
PAGE='https://www.w3schools.com/html/html5_video.asp'
SOURCE='https://www.w3schools.com/html/mov_bbb.mp4'

def binary_pids():
    result=[]
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            raw=(entry/'cmdline').read_bytes().split(b'\0')
            if raw and raw[0]==str(BIN).encode() and b'--native-host' not in raw:
                result.append(int(entry.name))
        except (FileNotFoundError,PermissionError,ValueError):
            pass
    return result

def wait_db_schema(db, timeout=30):
    deadline=time.time()+timeout
    last=None
    while time.time()<deadline:
        try:
            with sqlite3.connect(db) as connection:
                connection.execute('SELECT 1 FROM jobs LIMIT 1').fetchall()
            return
        except sqlite3.Error as error:
            last=error; time.sleep(0.2)
    raise RuntimeError(f'cold-start database schema did not become ready: {last}')

def main():
    root=Path(tempfile.mkdtemp(prefix='dm-cold-start-chromium-')); home=root/'home'; profile=root/'profile'; downloads=profile/'Default'/'Downloads'; home.mkdir(parents=True)
    public.write_native_manifest(str(home),str(profile)); inspector=support.free_port(); chrome_port=support.free_port(); xvfb,display=hls.start_xvfb(); support.DISPLAY=display
    chrome=None; client=None; resident_before=set(binary_pids())
    try:
        assert not resident_before, f'resident already running: {resident_before}'
        chrome=subprocess.Popen([str(public.CHROME),'--headless=new','--no-sandbox','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-allow-origins=*',f'--remote-debugging-port={chrome_port}',f'--user-data-dir={profile}',f'--load-extension={EXTENSION}',f'--disable-extensions-except={EXTENSION}','--window-size=1280,1000','--autoplay-policy=no-user-gesture-required','about:blank'],env=public.browser_env(str(home),inspector),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        public.wait_chrome(chrome_port); client=public.connect_chrome(chrome_port); public.wait_page(client,PAGE); player=public.wait_public_player(client); print('COLD-START-PLAYER:',json.dumps(player,sort_keys=True),flush=True)
        point=json.loads(client.evaluate("JSON.stringify((()=>{const b=document.querySelector('#dm-media-download-button');const r=b?.getBoundingClientRect();return r?{x:r.left+r.width/2,y:r.top+r.height/2}:null})())")); assert point
        client.call('Input.dispatchMouseEvent',{'type':'mousePressed','x':point['x'],'y':point['y'],'button':'left','clickCount':1,'modifiers':0}); client.call('Input.dispatchMouseEvent',{'type':'mouseReleased','x':point['x'],'y':point['y'],'button':'left','clickCount':1,'modifiers':0})
        db=public.db_path(str(home)); wait_db_schema(db); job=public.wait_job(db,SOURCE,90); started=set(binary_pids())-resident_before; assert started, f'native host did not launch resident: {binary_pids()}'; print('COLD-START-RESIDENT: PASS',sorted(started),flush=True)
        destination=str(root/'Downloads'/'cold-start.mp4'); public.commit_via_cli(str(home),inspector,job['id'],'cold-start.mp4',destination); done=public.wait_completed(db,job['id'],120); size,ref_hash=public.sha256_browser(client,SOURCE); assert Path(destination).stat().st_size==size; assert support.sha256(destination)==ref_hash
        ownership=public_hls.browser_ownership(client); assert ownership['context'] and ownership['context']['isTrusted'] and not ownership['context']['defaultPrevented']; assert ownership['click'] and ownership['click']['isTrusted'] and ownership['click']['ctrlKey'] and not ownership['click']['defaultPrevented']; assert len(public.jobs(db))==1
        files=[str(p.relative_to(profile)) for p in downloads.rglob('*')] if downloads.exists() else []; assert not files
        print('BROWSER-OWNERSHIP: PASS',json.dumps(ownership,sort_keys=True),flush=True); print(f'COLD-START-CHROMIUM: PASS (bytes={size},sha256={ref_hash},jobs=1,browser_downloads={files})',flush=True); print('COLD-START-CHROMIUM-PROBE: PASS',flush=True); return 0
    finally:
        if client is not None: client.sock.close()
        if chrome is not None and chrome.poll() is None: chrome.terminate(); chrome.wait(timeout=15)
        for pid in binary_pids():
            if pid not in resident_before:
                try: os.kill(pid,signal.SIGTERM)
                except ProcessLookupError: pass
        if xvfb.poll() is None: xvfb.terminate(); xvfb.wait(timeout=10)
        if os.environ.get('DM_KEEP_COLD_START')!='1': shutil.rmtree(root,ignore_errors=True)

if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error: print(f'COLD-START-CHROMIUM-PROBE: FAIL: {error}',flush=True); raise
