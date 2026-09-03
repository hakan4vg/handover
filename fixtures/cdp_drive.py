#!/usr/bin/env python3
"""Stdlib-only CDP driver: navigate, run JS, screenshot (no third-party deps).

Usage:
  dm-cdp.py <port> <url> <out.png> [<js-before-shot> ...]
Each JS arg is evaluated in order; the last screenshot is saved.
Prints each JS result (truncated) to stdout.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import urllib.request

PORT, URL, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
SCRIPTS = sys.argv[4:]


def ws_connect(port, path):
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("handshake failed")
        head += chunk
    if "101" not in head.decode("latin1").split("\r\n")[0]:
        raise RuntimeError(f"handshake rejected: {head[:80]!r}")
    return sock


def recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError("connection closed")
        data += chunk
    return data


def ws_send(sock, payload):
    raw = payload if isinstance(payload, bytes) else payload.encode()
    mask = os.urandom(4)
    head = bytes([0x81, 0x80 | min(len(raw), 126)])
    if len(raw) > 125:
        head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(raw))
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(raw)))


def ws_recv(sock):
    chunks, opcode = [], None
    while True:
        first, second = recv_exact(sock, 2)
        fin, op = first & 0x80, first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", recv_exact(sock, 8))[0]
        if second & 0x80:
            recv_exact(sock, 4)  # server frames are unmasked; skip if set
        data = recv_exact(sock, length)
        if op == 0x8:
            raise RuntimeError("server closed connection")
        if op == 0x9:  # ping -> pong
            ws_send(sock, b"")
            continue
        if op in (0x1, 0x2):
            opcode = opcode or op
            chunks.append(data)
        elif op == 0x0:
            chunks.append(data)
        if fin:
            body = b"".join(chunks)
            return body.decode("utf-8")


class CDP:
    def __init__(self, sock):
        self.sock = sock
        self.msg_id = 0

    def call(self, method, params=None, timeout=60):
        import time

        self.msg_id += 1
        ws_send(
            self.sock,
            json.dumps({"id": self.msg_id, "method": method, "params": params or {}}),
        )
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise RuntimeError(f"timeout waiting for {method}")
            reply = json.loads(ws_recv(self.sock))
            if reply.get("id") == self.msg_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def evaluate(self, expression):
        res = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        remote = res.get("result", {}).get("result", {})
        return remote.get("value", remote.get("description"))


def main():
    targets = json.load(
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=15)
    )
    page = next(t for t in targets if t["type"] == "page")
    ws_url = page["webSocketDebuggerUrl"]
    path = ws_url.split(f"127.0.0.1:{PORT}", 1)[1]
    cdp = CDP(ws_connect(PORT, path))
    cdp.call("Page.enable")
    cdp.call("Page.navigate", {"url": URL})
    import time

    for _ in range(60):
        time.sleep(0.5)
        try:
            state = cdp.evaluate("document.readyState")
        except RuntimeError:
            continue
        if state == "complete":
            break
    time.sleep(2)  # let React render + mock timeouts fire
    for script in SCRIPTS:
        try:
            print("JS:", json.dumps(cdp.evaluate(script))[:1500], flush=True)
        except RuntimeError as exc:
            print(f"JS-ERROR: {exc}", flush=True)
        time.sleep(1)
    shot = cdp.call("Page.captureScreenshot", {"format": "png"})
    with open(OUT, "wb") as handle:
        handle.write(base64.b64decode(shot["data"]))
    print(f"saved {OUT}", flush=True)


if __name__ == "__main__":
    main()
