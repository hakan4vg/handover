#!/usr/bin/env python3
"""CDP helper: capture Chrome's own compositor screenshot + run page JS.

Usage: cdp_shot.py <out.png> [<js-expression>]
Prints the JS result (if given) to stdout.
"""
import json
import sys
import urllib.request

import websocket

OUT = sys.argv[1]
EXPR = sys.argv[2] if len(sys.argv) > 2 else None

targets = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list"))
page = next(t for t in targets if t["type"] == "page" and "video.html" in t["url"])
ws = websocket.create_connection(page["webSocketDebuggerUrl"])
msg_id = [0]


def send(method, params=None):
    msg_id[0] += 1
    ws.send(json.dumps({"id": msg_id[0], "method": method, "params": params or {}}))
    while True:
        reply = json.loads(ws.recv())
        if reply.get("id") == msg_id[0]:
            return reply.get("result", {})


if EXPR is not None:
    res = send("Runtime.evaluate", {"expression": EXPR, "returnByValue": True})
    value = res.get("result", {}).get("result", {}).get("value")
    print("JS:", json.dumps(value)[:2000])

send("Page.enable")
shot = send("Page.captureScreenshot", {"format": "png"})
data = bytes.fromhex("") if not shot.get("data") else __import__("base64").b64decode(shot["data"])
with open(OUT, "wb") as handle:
    handle.write(data)
print("saved", OUT, len(data), "bytes")
ws.close()
