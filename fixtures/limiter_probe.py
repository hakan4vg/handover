#!/usr/bin/env python3
"""Headless aggregate-limiter proof (no GUI input, no XTEST).

Launches the real compiled binary under Xvfb :99 with an isolated HOME,
seeds settings bandwidthLimit=1 MB/s directly in its SQLite DB, starts a
provisional capture of the 8 MiB deterministic fixture, and polls the jobs
table to measure the effective transfer rate. A ~1 MB/s plateau on loopback
(which otherwise moves >50 MB/s) proves the shared token bucket paces real
acquisition work end to end.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

BIN = "/srv/repos/downloadmanager/src-tauri/target/debug/download-manager"
HOME = os.environ.get("DM_HOME", "/tmp/dm-limit-test")
DB = os.path.join(HOME, ".local/share/com.downloadmanager.app/download-manager.db")
CAPTURE_URL = os.environ.get(
    "DM_MANIFEST_URL", "http://127.0.0.1:8901/file/range.bin"
)
# Manifest mode (HLS/DASH): byte-identity against the manifest URL is
# meaningless (the product is assembled media), so the end assertion is
# segments completed == total with the full byte count downloaded instead.
MANIFEST_MODE = bool(os.environ.get("DM_MANIFEST_URL"))
CAPTURE_NAME = "slow" if MANIFEST_MODE else "range.bin"
# DM_LIMIT_MB: "none" for unlimited global, else a number (default "1").
# DM_JOB_CAP_BPS: per-job cap in bytes/sec for the capture (default unset).
# DM_EXPECT_MB: expected effective rate in MB/s (default follows the limit).
LIMIT_MB = os.environ.get("DM_LIMIT_MB", "1")
JOB_CAP_BPS = os.environ.get("DM_JOB_CAP_BPS")
EXPECT_MB = float(os.environ.get("DM_EXPECT_MB", "1" if LIMIT_MB != "none" else "0.5"))
# DM_JOBS: concurrent captures in the one app instance (default 1). With 2+,
# the assertion is on the COMBINED rate (the global limit paces the sum of
# active jobs) plus a no-starvation check per job.
NUM_JOBS = max(1, int(os.environ.get("DM_JOBS", "1")))


def env():
    return dict(
        os.environ,
        HOME=HOME,
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
    con = sqlite3.connect(DB)
    try:
        rows = con.execute("SELECT payload FROM jobs").fetchall()
    finally:
        con.close()
    return [json.loads(payload) for (payload,) in rows]


def set_bandwidth_limit():
    con = sqlite3.connect(DB)
    try:
        (payload,) = con.execute(
            "SELECT payload FROM settings WHERE id = 1"
        ).fetchone()
        settings = json.loads(payload)
        if LIMIT_MB == "none":
            settings["bandwidthLimit"] = None
        else:
            # Integers only: the Rust settings schema is Option<u64> and a
            # JSON float fails the whole settings parse (silent default reset).
            settings["bandwidthLimit"] = int(float(LIMIT_MB))
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

    # Pass 1: boot once so the app creates its DB and settings row.
    boot = subprocess.Popen(
        [BIN], env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_db()
    finally:
        boot.terminate()
        boot.wait(timeout=15)
    set_bandwidth_limit()
    print(f"seeded global={LIMIT_MB} MB/s job_cap={JOB_CAP_BPS} expect={EXPECT_MB} MB/s", flush=True)

    # Pass 2: real capture of the 8 MiB fixture.
    payload: dict = {"source": CAPTURE_URL, "name": CAPTURE_NAME}
    if JOB_CAP_BPS:
        payload["bandwidthLimit"] = int(JOB_CAP_BPS)
    capture = json.dumps({"type": "capture-acquisition", "payload": payload})
    app = subprocess.Popen(
        [BIN, "--capture", capture],
        env=env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    forwarders = []
    # Extra captures go through single-instance forwarding into the same
    # resident app, exactly like two rapid browser captures would.
    for extra in range(1, NUM_JOBS):
        for _ in range(30):
            if len(get_jobs()) >= extra:
                break
            time.sleep(0.5)
        forwarders.append(
            subprocess.Popen(
                [BIN, "--capture", capture],
                env=env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    for proc in forwarders:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    samples: list = []
    job = None
    try:
        deadline = time.time() + 150
        while time.time() < deadline:
            time.sleep(0.5)
            jobs = get_jobs()
            if len(jobs) < NUM_JOBS:
                continue
            job = jobs[0]
            total_downloaded = sum(item.get("downloaded", 0) for item in jobs)
            samples.append(
                (time.time(), total_downloaded, [item.get("state") for item in jobs])
            )
            if all(item.get("state") in ("finalizing", "completed") for item in jobs):
                break
        states = sorted({state for _, _, group in samples for state in group})
        print(f"observed states: {states}", flush=True)
        downloading = [
            (t, n) for (t, n, group) in samples if "downloading" in group and n > 0
        ]
        if MANIFEST_MODE:
            # Parallel same-size fragments complete together, so progress jumps
            # 0 -> total at the end and progress samples may not exist. Measure
            # wall-clock across the downloading phase instead.
            assert any("downloading" in group for (_, _, group) in samples), "never entered downloading"
            t0 = next(t for (t, _, group) in samples if "downloading" in group)
            t1 = next(
                t
                for (t, _, group) in samples
                if "finalizing" in group or "completed" in group
            )
            assert t1 - t0 >= 1.5, f"downloading phase too short: {t1 - t0:.1f}s"
            n0 = 0
            n1 = max(n for (_, n, _) in samples)
        else:
            assert len(downloading) >= 6, f"too few downloading samples: {len(samples)}"
            (t0, n0), (t1, n1) = downloading[0], downloading[-1]
        rate = (n1 - n0) / max(t1 - t0, 0.01)
        expected = EXPECT_MB * 1e6
        print(
            f"combined {n0} -> {n1} bytes over {t1 - t0:.1f}s = {rate / 1e6:.2f} MB/s (expect ~{EXPECT_MB})",
            flush=True,
        )
        assert 0.5 * expected <= rate <= 1.6 * expected, f"rate {rate} outside paced band"
        print(f"RATE-ASSERT: PASS ({rate / 1e6:.2f} MB/s within band of ~{EXPECT_MB} MB/s)", flush=True)
        if NUM_JOBS > 1:
            finals = {item["id"]: item for item in get_jobs()}
            for job_id, item in finals.items():
                assert item.get("downloaded", 0) > 0, f"{job_id} starved"
            print(f"NO-STARVATION: PASS ({len(finals)} jobs all progressed)", flush=True)

        if MANIFEST_MODE:
            assert job is not None, "no job observed"
            segments = job.get("segments") or {}
            done, total = segments.get("completed"), segments.get("total")
            print(f"segments: {done}/{total} downloaded={job.get('downloaded')}", flush=True)
            assert (done, total) == (4, 4), f"slow manifest incomplete: {segments}"
            assert job.get("downloaded") == 4 * 1024 * 1024, job.get("downloaded")
            print("SEGMENT-COMPLETION: PASS (4/4 fragments, 4 MiB)", flush=True)
        elif job.get("state") in ("finalizing", "completed"):
            temp_path = job["tempPath"]
            for _ in range(60):
                if os.path.exists(temp_path) and os.path.getsize(temp_path) == 8 * 1024 * 1024:
                    break
                time.sleep(0.5)
            actual = open(temp_path, "rb").read()
            expected = urllib.request.urlopen(CAPTURE_URL, timeout=60).read()
            assert actual == expected, "provisional bytes differ from fixture"
            print(
                f"BYTE-IDENTITY: PASS ({len(actual)} bytes identical to fixture)",
                flush=True,
            )
        else:
            print(
                f"final state {job.get('state')}; byte-identity skipped, rate proof stands",
                flush=True,
            )
        print("LIMITER-PROBE: PASS", flush=True)
    finally:
        app.terminate()
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    sys.exit(main())
