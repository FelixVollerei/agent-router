"""Explicit, bounded HTTP reads in a supervised child; never follows redirects."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

from .monitor import RunCancelled
from .process import LineProcess


def validate_url(url, allow_loopback=False):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise ValueError("invalid_url")
    p = urlsplit(url)
    if not p.hostname or p.username or p.password or p.fragment:
        raise ValueError("credential_or_invalid_url")
    local = p.hostname in {"localhost", "127.0.0.1", "::1"}
    if p.scheme != "https" and not (allow_loopback and local and p.scheme == "http"):
        raise ValueError("https_required")
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError("invalid_port")
    return p


def fetch_raw(spec):
    p = validate_url(spec["url"], spec.get("allow_loopback", False))
    port = p.port or (443 if p.scheme == "https" else 80)
    addresses = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("no_address")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global and not (spec.get("allow_loopback") and ip.is_loopback):
            raise ValueError("non_public_address")
    # Pin the validated address; TLS still verifies the original hostname.
    address = addresses[0][4]
    timeout = min(30, max(.1, float(spec.get("timeout", 15))))
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = cls(p.hostname, port=port, timeout=timeout)
    conn._create_connection = lambda *a, **kw: socket.create_connection(address[:2], timeout)
    headers = {"User-Agent": "EngineeringModelRouter/0.4", "Accept": "text/html,application/json,text/plain", "Accept-Encoding": "identity"}
    env = spec.get("api_key_env")
    if env:
        key = os.environ.get(env)
        if not key:
            raise ValueError("credential_unavailable")
        headers[spec.get("key_header", "Authorization")] = spec.get("key_prefix", "Bearer ") + key
    limit = min(160000, max(1, int(spec.get("max_bytes", 160000))))
    try:
        conn.request("GET", (p.path or "/") + ("?" + p.query if p.query else ""), headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"http_{response.status}")
        if response.getheader("Content-Encoding", "identity") not in {"", "identity"}:
            raise ValueError("compressed_response_unsupported")
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("response_size_limit")
        return {"text": raw.decode("utf-8", errors="replace"), "bytes": len(raw),
                "content_type": response.getheader("Content-Type", "").split(";")[0].lower(),
                "sha256": hashlib.sha256(raw).hexdigest()}
    finally:
        conn.close()


def fetch(spec, timeout=20, cancel_event=None):
    validate_url(spec["url"], spec.get("allow_loopback", False))
    child = LineProcess([sys.executable, "-B", "-X", "utf8", "-m", "model_router.network"], Path(__file__).resolve().parent.parent)
    start = time.monotonic()
    child.write_input(json.dumps({**spec, "timeout": timeout}))
    try:
        while time.monotonic() - start < timeout:
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelled("Network read cancelled")
            item = child.poll(.05)
            if item and item[0] == "stdout":
                value = json.loads(item[1])
                if "error" in value:
                    raise ValueError(value["error"])
                return value["result"]
            if item and item[0] == "overflow":
                raise ValueError("network_output_limit")
            if child.finished():
                raise ValueError("network_worker_exited")
        raise TimeoutError("network_deadline")
    finally:
        child.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("request_size_limit")
        result = fetch_raw(json.loads(raw))
        # Keep JSONL below LineProcess's 256KiB frame cap, including escaped text.
        encoded = json.dumps({"result": result}, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 250000:
            raise ValueError("network_output_limit")
        print(encoded)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"error": reason[:160]}))
