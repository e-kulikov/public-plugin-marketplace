"""Talk to Chrome over the DevTools protocol.

* ``AgentBrowser`` runs JavaScript in the ChatGPT tab through the ``agent-browser``
  CLI (the plugin dependency): ``agent-browser --cdp URL eval --stdin``.
* ``DownloadHolder`` keeps a raw CDP session open with
  ``Browser.setDownloadBehavior: allow`` so Chrome saves files into a chosen
  folder without asking "allow multiple downloads?". The setting lasts only while
  the session is open, hence a small background process. It needs only the
  standard library (minimal WebSocket client below).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
from urllib.parse import urlparse


class CdpError(RuntimeError):
    pass


def http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def cdp_alive(base_url, timeout=2):
    try:
        return bool(http_json(base_url.rstrip("/") + "/json/version", timeout).get("webSocketDebuggerUrl"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# agent-browser wrapper
# ---------------------------------------------------------------------------

class AgentBrowser:
    def __init__(self, cdp_url, binary=None):
        self.cdp_url = cdp_url
        self.binary = binary or shutil.which("agent-browser")
        if not self.binary:
            raise CdpError("agent-browser CLI not found. Install it: npm i -g agent-browser && agent-browser install")

    def _env(self):
        env = dict(os.environ)
        # A CA bundle forces "locally launched Chromium" mode, which rejects --cdp.
        env.pop("AGENT_BROWSER_CA_CERT", None)
        return env

    def _run(self, args, stdin=None, timeout=120):
        cmd = [self.binary, "--cdp", self.cdp_url] + args
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=self._env())
        if r.returncode != 0:
            raise CdpError("agent-browser %s failed: %s" % (" ".join(args[:2]), (r.stderr or r.stdout).strip()[:500]))
        return r.stdout

    def eval_json(self, js, timeout=120):
        """Run ``js`` (an expression returning a JSON-serialisable value) in the active tab."""
        wrapped = "(async()=>JSON.stringify(await (%s)))()" % js.strip().rstrip(";")
        out = self._run(["eval", "--stdin"], stdin=wrapped, timeout=timeout)
        try:
            val = json.loads(out)  # agent-browser prints the returned string as JSON
            return json.loads(val) if isinstance(val, str) else val
        except ValueError as e:
            raise CdpError("unexpected agent-browser output: %r (%s)" % (out[:200], e))

    def open(self, url):
        self._run(["open", url])

    def current_url(self):
        return self.eval_json("location.href")


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455, text frames only) + download holder
# ---------------------------------------------------------------------------

class MiniWebSocket:
    def __init__(self, url, timeout=10):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (path, u.hostname, u.port, key, )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("websocket handshake failed (connection closed)")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise CdpError("websocket handshake refused: %s" % buf.split(b"\r\n", 1)[0].decode(errors="replace"))
        self._buf = buf.split(b"\r\n\r\n", 1)[1]

    def send_text(self, text):
        data = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(data)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Next text message (handles fragmentation and ping/close); None on close."""
        msg = b""
        while True:
            b0, b1 = self._read(2)
            op, ln = b0 & 0x0F, b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(ln)
            if op == 0x8:
                return None
            if op == 0x9:  # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if op in (0x1, 0x0, 0x2):
                msg += payload
                if b0 & 0x80:
                    return msg.decode("utf-8", "replace")

    def settimeout(self, t):
        self.sock.settimeout(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def set_download_behavior(cdp_base_url, download_dir):
    """Open a browser-level CDP session and allow downloads into ``download_dir``.

    Returns the open ``MiniWebSocket``; the caller must keep it alive.
    """
    ver = http_json(cdp_base_url.rstrip("/") + "/json/version", 5)
    ws_url = ver["webSocketDebuggerUrl"]
    # The URL may name a host that is not reachable from here (e.g. 127.0.0.1 behind a proxy).
    base = urlparse(cdp_base_url)
    u = urlparse(ws_url)
    ws_url = "ws://%s:%s%s" % (base.hostname, base.port, u.path)
    ws = MiniWebSocket(ws_url)
    ws.send_text(json.dumps({"id": 1, "method": "Browser.setDownloadBehavior",
                             "params": {"behavior": "allow", "downloadPath": download_dir,
                                        "eventsEnabled": False}}))
    ws.settimeout(10)
    reply = ws.recv()
    if reply is None or '"error"' in reply:
        raise CdpError("Browser.setDownloadBehavior rejected: %s" % reply)
    return ws


def holder_main(cdp_base_url, download_dir, stop_file, parent_pid=None):
    """Background loop: keep the session open until ``stop_file`` appears or the parent dies."""
    ws = set_download_behavior(cdp_base_url, download_dir)
    ws.settimeout(1.0)
    try:
        while not os.path.exists(stop_file):
            if parent_pid and not _pid_alive(parent_pid):
                break
            try:
                if ws.recv() is None:
                    break
            except socket.timeout:
                continue
            except CdpError:
                break
    finally:
        ws.close()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_holder(cdp_base_url, download_dir, state_dir):
    """Launch the holder as a detached process; returns ``(pid, stop_file)`` once it is ready."""
    import sys
    os.makedirs(state_dir, exist_ok=True)
    stop_file = os.path.join(state_dir, "holder.stop")
    ready_file = os.path.join(state_dir, "holder.ready")
    for f in (stop_file, ready_file):
        if os.path.exists(f):
            os.unlink(f)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys; sys.path.insert(0, %r); from chatgpt_backup import cdp; "
            "import os; "
            "ws = cdp.set_download_behavior(%r, %r); open(%r,'w').write(str(os.getpid())); ws.settimeout(1.0)\n"
            "import socket\n"
            "while not os.path.exists(%r) and cdp._pid_alive(%d):\n"
            "    try:\n"
            "        if ws.recv() is None: break\n"
            "    except socket.timeout: pass\n"
            "    except Exception: break\n"
            % (here, cdp_base_url, download_dir, ready_file, stop_file, os.getpid()))
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if os.path.exists(ready_file):
            return p.pid, stop_file
        if p.poll() is not None:
            raise CdpError("download holder exited early (could not set download behavior)")
        time.sleep(0.1)
    raise CdpError("download holder did not become ready")


def stop_holder(stop_file):
    try:
        open(stop_file, "w").close()
    except OSError:
        pass
