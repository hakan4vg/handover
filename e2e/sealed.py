"""The extension's side of the sealed bridge channel, for the harness
(extension/src/sealed.ts and src-tauri/src/pairing.rs are the real sides)."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import devtools

BRIDGE = "http://127.0.0.1:38217"
REQUEST_AAD = b"dm-bridge-1 request"
RESPONSE_AAD = "dm-bridge-1 response "


def post(path: str, body: bytes | dict, headers: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(BRIDGE + path, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def pair(allow: bool = True) -> dict:
    """Pair the way the extension does, answering in the app's pair window
    through the core command its buttons call. Returns the status answer."""
    request = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=").replace("_", "-")
    status, begun = post("/v1/pair", {"request": request})
    if status != 200 or not begun.get("ok"):
        return {"begin": begun}
    devtools.invoke("answer_pairing", {"id": request, "allow": allow})
    _, answer = post("/v1/pair/status", {"request": request})
    return {**answer, "code": begun.get("code"), "request": request}


def seal(pairing: dict, message: dict, sent_at: float | None = None) -> tuple[bytes, str]:
    nonce = os.urandom(12)
    plain = json.dumps({"t": int((sent_at or time.time()) * 1000), "message": message}).encode()
    data = AESGCM(base64.b64decode(pairing["key"])).encrypt(nonce, plain, REQUEST_AAD)
    iv = base64.b64encode(nonce).decode()
    return json.dumps({"k": pairing["keyId"], "iv": iv, "data": base64.b64encode(data).decode()}).encode(), iv


def open_answer(pairing: dict, nonce: str, envelope: dict) -> dict:
    plain = AESGCM(base64.b64decode(pairing["key"])).decrypt(base64.b64decode(envelope["iv"]), base64.b64decode(envelope["data"]), (RESPONSE_AAD + nonce).encode())
    return json.loads(plain)


def message(pairing: dict, body: dict, timeout: float = 30, origin: str | None = None) -> dict:
    """Send one sealed message; return the opened answer plus its HTTP status."""
    sealed, nonce = seal(pairing, body)
    status, envelope = post("/v1/message", sealed, {"Origin": origin} if origin else None, timeout)
    if "iv" not in envelope:
        return {"_status": status, **envelope}
    return {"_status": status, **open_answer(pairing, nonce, envelope)}
