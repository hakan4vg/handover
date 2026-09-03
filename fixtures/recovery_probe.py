#!/usr/bin/env python3
"""Crash-recovery proof (no GUI input, no XTEST).

SPEC §7.3: uncommitted provisional acquisitions need no crash durability —
orphaned provisional cache data is cleaned on next launch. Committed jobs
must survive with valid completed work intact (SPEC §8.7).

1. Seeds a committed paused range job (real first MiB of the deterministic
   fixture + matching completed range) directly in the app DB.
2. Boots the app, starts a slow-HLS capture, SIGTERMs mid-download.
3. Reboots and asserts: no provisional rows remain, no provisional temp
   files or segment dirs remain, the seeded committed job is intact with
   its completed range, and the app stays alive.
"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

BIN = "/srv/repos/downloadmanager/src-tauri/target/debug/download-manager"
HOME = os.environ.get("DM_HOME", "/tmp/dm-recovery-test")
ROOT = os.path.join(HOME, ".local/share/com.downloadmanager.app")
DB = os.path.join(ROOT, "download-manager.db")
RANGE_URL = "http://127.0.0.1:8901/file/range.bin"
SLOW_URL = "http://127.0.0.1:8901/hls/slow.m3u8"


def env():
    return dict(
        os.environ,
        HOME=HOME,
        # Scope the temp dir too: the app resolves it via the cache dir,
        # which follows XDG_CACHE_HOME, not HOME.
        XDG_CACHE_HOME=os.path.join(HOME, ".cache"),
        DISPLAY=":99",
        WEBKIT_DISABLE_COMPOSITING_MODE="1",
    )


def wait_db(timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(DB):
            return
        time.sleep(0.5)
    raise RuntimeError("database never appeared")


def get_jobs():
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(DB)
    try:
        rows = con.execute("SELECT payload FROM jobs").fetchall()
    finally:
        con.close()
    return [json.loads(payload) for (payload,) in rows]


def seed_committed_job():
    first_mib = urllib.request.Request(RANGE_URL, headers={"Range": "bytes=0-1048575"})
    with urllib.request.urlopen(first_mib, timeout=30) as response:
        assert response.status == 206, response.status
        data = response.read()
    assert len(data) == 1024 * 1024, len(data)
    temp_path = os.path.join(HOME, "seeded-1.part")
    with open(temp_path, "wb") as handle:
        handle.write(data)
    job = {
        "id": "seeded-1",
        "name": "range.bin",
        "source": RANGE_URL,
        "domain": "127.0.0.1",
        "kind": "document",
        "state": "paused",
        "progress": 12.5,
        "downloaded": 1024 * 1024,
        "total": 8 * 1024 * 1024,
        "speed": 0,
        "eta": "Paused",
        "connections": 0,
        "maxConnections": 8,
        "bandwidthLimit": None,
        "mode": "whole-object",
        "media": False,
        "destination": os.path.join(HOME, "Downloads", "range.bin"),
        "tempPath": temp_path,
        "resumable": True,
        "created": "Just now",
        "provisional": False,
        "completedRanges": [{"start": 0, "end": 1024 * 1024 - 1}],
        "events": [],
    }
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT INTO jobs (id, created_at, payload) VALUES (?, ?, ?)",
            ("seeded-1", "Just now", json.dumps(job)),
        )
        con.commit()
    finally:
        con.close()
    print("seeded committed paused job (1 MiB verified range)", flush=True)


def seed_limit():
    con = sqlite3.connect(DB)
    try:
        (payload,) = con.execute(
            "SELECT payload FROM settings WHERE id = 1"
        ).fetchone()
        settings = json.loads(payload)
        settings["bandwidthLimit"] = 1
        settings["bandwidthUnit"] = "MB/s"
        con.execute(
            "UPDATE settings SET payload = ? WHERE id = 1",
            (json.dumps(settings),),
        )
        con.commit()
    finally:
        con.close()


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)

    boot = subprocess.Popen(
        [BIN], env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_db()
    finally:
        boot.terminate()
        boot.wait(timeout=15)
    seed_committed_job()
    seed_limit()

    capture = json.dumps(
        {
            "type": "capture-acquisition",
            "payload": {"source": SLOW_URL, "name": "slow"},
        }
    )
    app = subprocess.Popen(
        [BIN, "--capture", capture],
        env=env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(0.25)
            prov = [
                job
                for job in get_jobs()
                if job.get("provisional") and job.get("state") == "downloading"
                and job.get("downloaded", 0) > 0
            ]
            if prov:
                break
        else:
            raise RuntimeError("provisional never progressed")
        print(
            f"killing mid-download at {prov[0]['downloaded']} bytes", flush=True
        )
    finally:
        app.terminate()
        app.wait(timeout=15)

    relaunch = subprocess.Popen(
        [BIN], env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(8)
        assert relaunch.poll() is None, "relaunch died"
        jobs = get_jobs()
        provisionals = [job for job in jobs if job.get("provisional")]
        assert not provisionals, f"provisional rows survived: {provisionals}"
        print("PROVISIONAL-SWEEP: PASS (no provisional rows after crash)", flush=True)
        leftovers = glob.glob(os.path.join(HOME, "**", "provisional-*.part*"), recursive=True)
        segdirs = glob.glob(os.path.join(HOME, "**", "*.segments"), recursive=True)
        assert not leftovers, leftovers
        assert not segdirs, segdirs
        print("TEMP-SWEEP: PASS (no provisional temp files or segment dirs)", flush=True)
        seeded = [job for job in jobs if job["id"] == "seeded-1"]
        assert len(seeded) == 1, "seeded committed job missing"
        assert seeded[0]["state"] == "paused", seeded[0]["state"]
        assert seeded[0]["completedRanges"] == [
            {"start": 0, "end": 1024 * 1024 - 1}
        ], seeded[0]["completedRanges"]
        assert seeded[0]["downloaded"] == 1024 * 1024, seeded[0]["downloaded"]
        assert os.path.getsize(os.path.join(HOME, "seeded-1.part")) == 1024 * 1024
        print("COMMITTED-SURVIVAL: PASS (paused job + verified range intact)", flush=True)
        print("RECOVERY-PROBE: PASS", flush=True)
    finally:
        relaunch.terminate()
        try:
            relaunch.wait(timeout=15)
        except subprocess.TimeoutExpired:
            relaunch.kill()


if __name__ == "__main__":
    sys.exit(main())
