"""Tiny Discord gateway client (real-time events) using only the standard library.

Used by the deleted-messages logger. It keeps ONE connection open and sleeps until
Discord sends something, so it uses almost no CPU or bandwidth:
  - a heartbeat every ~41 seconds
  - the messages of your DMs as they arrive
"""
import base64
import json
import os
import random
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"
FATAL_CLOSE = {4004: "Your token is wrong or expired.", 4010: "Invalid shard.",
               4011: "Sharding required.", 4012: "Invalid API version.",
               4013: "Invalid intents.", 4014: "Disallowed intents."}


class Closed(Exception):
    def __init__(self, code=None, reason=""):
        super().__init__(f"connection closed ({code}) {reason}")
        self.code = code


class WebSocket:
    """Minimal RFC 6455 client: text frames, ping/pong, close. No compression."""

    def __init__(self, url, timeout=20):
        u = urlparse(url)
        secure = u.scheme == "wss"
        host, port = u.hostname, u.port or (443 if secure else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nOrigin: https://discord.com\r\n"
               f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n\r\n")
        self.sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("Discord refused the real-time connection: "
                                  + head.split(b"\r\n", 1)[0].decode(errors="replace"))

    def _read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Closed(None, "eof")
            self.buf += chunk
        head, self.buf = self.buf.split(marker, 1)
        return head

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise Closed(None, "eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload=b""):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj, separators=(",", ":")).encode())

    def recv_json(self, timeout):
        """Next text message as an object, or None if nothing came within `timeout`."""
        self.sock.settimeout(timeout)
        parts = []
        while True:
            try:
                if not self.buf:
                    first = self.sock.recv(4096)
                    if not first:
                        raise Closed(None, "eof")
                    self.buf += first
                self.sock.settimeout(30)          # a frame has started: read it whole
                b1, b2 = self._read_exact(2)
            except (socket.timeout, TimeoutError):
                if parts:
                    continue
                return None
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            data = self._read_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
                raise Closed(code, data[2:].decode(errors="replace"))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return json.loads(b"".join(parts).decode("utf-8"))
            self.sock.settimeout(30)

    def close(self, code=1000):
        try:
            self._send_frame(0x8, struct.pack("!H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class GatewaySession:
    """Connects, identifies, heartbeats, resumes and reconnects by itself.

    on_event(name, data) is called for every dispatch event.
    should_stop() is checked about once per second.
    """

    def __init__(self, token, on_event, should_stop, log=lambda t: None, url=GATEWAY,
                 on_status=lambda s: None):
        self.token = token
        self.on_event = on_event
        self.should_stop = should_stop
        self.log = log
        self.url = url
        self.resume_url = None
        self.session_id = None
        self.seq = None
        self.on_status = on_status
        self.fatal = None

    def run_forever(self):
        backoff = 5
        while not self.should_stop() and not self.fatal:
            try:
                self._run_once()
                backoff = 5
            except Closed as e:
                if e.code in FATAL_CLOSE:
                    self.fatal = FATAL_CLOSE[e.code]
                    self.on_status("error: " + self.fatal)
                    self.log(self.fatal)
                    return
                if e.code in (4007, 4009):        # bad seq / session timed out: start over
                    self.session_id = self.seq = None
                self.log(f"Disconnected ({e.code}), reconnecting…")
            except (OSError, ValueError, ConnectionError) as e:
                self.log(f"Connection problem: {e}. Retrying in {backoff}s")
                self.on_status("reconnecting")
                end = time.time() + backoff
                while time.time() < end and not self.should_stop():
                    time.sleep(1)
                backoff = min(backoff * 2, 300)

    def _run_once(self):
        url = self.resume_url if (self.session_id and self.resume_url) else self.url
        if "?" not in url:
            url += "/?v=9&encoding=json"
        ws = WebSocket(url)
        try:
            hello = ws.recv_json(30)
            if not hello or hello.get("op") != 10:
                raise ConnectionError("no hello from Discord")
            interval = hello["d"]["heartbeat_interval"] / 1000
            next_beat = time.time() + interval * random.random()
            acked = True
            if self.session_id:
                ws.send_json({"op": 6, "d": {"token": self.token, "session_id": self.session_id,
                                             "seq": self.seq}})
            else:
                self._identify(ws)
            while not self.should_stop():
                now = time.time()
                if now >= next_beat:
                    if not acked:
                        raise Closed(None, "no heartbeat ack")
                    ws.send_json({"op": 1, "d": self.seq})
                    acked = False
                    next_beat = now + interval
                msg = ws.recv_json(min(2.0, max(0.1, next_beat - now)))
                if msg is None:
                    continue
                op = msg.get("op")
                if msg.get("s") is not None:
                    self.seq = msg["s"]
                if op == 0:
                    t, d = msg.get("t"), msg.get("d") or {}
                    if t == "READY":
                        self.session_id = d.get("session_id")
                        self.resume_url = d.get("resume_gateway_url")
                        self.on_status("running")
                    elif t == "RESUMED":
                        self.on_status("running")
                    try:
                        self.on_event(t, d)
                    except Exception as e:          # never let one event kill the logger
                        self.log(f"Event error ({t}): {e}")
                elif op == 1:
                    ws.send_json({"op": 1, "d": self.seq})
                elif op == 11:
                    acked = True
                elif op == 7:
                    raise Closed(None, "reconnect requested")
                elif op == 9:
                    if not msg.get("d"):
                        self.session_id = self.seq = None
                    time.sleep(random.uniform(1, 5))
                    raise Closed(None, "invalid session")
        finally:
            ws.close()

    def _identify(self, ws):
        ws.send_json({"op": 2, "d": {
            "token": self.token,
            "capabilities": 0,
            "properties": {"os": "Windows", "browser": "Chrome", "device": "",
                           "system_locale": "en-US", "browser_user_agent":
                               "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            "presence": {"status": "unknown", "since": 0, "activities": [], "afk": False},
            "compress": False}})
