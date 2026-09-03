#!/usr/bin/env python3
"""Settings-recovery wiring proof (no GUI input).

Seeds the app DB with a corrupt settings row (float bandwidthLimit, custom
folders), boots the real binary under Xvfb :99, and checks the row the app
writes back: the corrupt key falls back to default while every valid key
survives. Before the fix the whole row reset to defaults.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

BIN = "/srv/repos/downloadmanager/src-tauri/target/debug/download-manager"
HOME = "/tmp/dm-settings-test"
DB = os.path.join(HOME, ".local/share/com.downloadmanager.app/download-manager.db")


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)
    env = dict(
        os.environ,
        HOME=HOME,
        DISPLAY=":99",
        WEBKIT_DISABLE_COMPOSITING_MODE="1",
    )

    boot = subprocess.Popen(
        [BIN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(60):
            if os.path.exists(DB):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("database never appeared")
    finally:
        boot.terminate()
        boot.wait(timeout=15)

    con = sqlite3.connect(DB)
    try:
        (payload,) = con.execute(
            "SELECT payload FROM settings WHERE id = 1"
        ).fetchone()
        settings = json.loads(payload)
        settings["defaultFolder"] = "/tmp/custom-downloads"
        settings["maxConnections"] = 4
        settings["bandwidthLimit"] = 1.5  # corrupt: schema wants u64
        con.execute(
            "UPDATE settings SET payload = ? WHERE id = 1",
            (json.dumps(settings),),
        )
        con.commit()
    finally:
        con.close()
    print("seeded corrupt row (float limit + custom folder)", flush=True)

    app = subprocess.Popen(
        [BIN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        assert app.poll() is None, "app exited immediately"
        time.sleep(10)
        assert app.poll() is None, "app died on corrupt settings"
        con = sqlite3.connect(DB)
        try:
            (payload,) = con.execute(
                "SELECT payload FROM settings WHERE id = 1"
            ).fetchone()
        finally:
            con.close()
        back = json.loads(payload)
        print(f"read back: folder={back['defaultFolder']} limit={back['bandwidthLimit']} maxConn={back['maxConnections']}", flush=True)
        assert back["defaultFolder"] == "/tmp/custom-downloads", back
        assert back["maxConnections"] == 4, back
        assert back["bandwidthLimit"] is None, back
        print("SETTINGS-RECOVERY-PROBE: PASS", flush=True)
    finally:
        app.terminate()
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    sys.exit(main())
