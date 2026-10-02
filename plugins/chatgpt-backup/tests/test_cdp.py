import base64
import hashlib
import json
import socket
import struct
import threading
import unittest

import helpers  # noqa: F401
from chatgpt_backup import cdp

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeBrowser(threading.Thread):
    """HTTP /json/version + a WebSocket endpoint that answers Browser.setDownloadBehavior."""

    def __init__(self, reply_error=False):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.reply_error = reply_error

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            head = req.split(b"\r\n\r\n")[0].decode()
            if head.startswith("GET /json/version"):
                body = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/browser/x" % self.port}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
                conn.close()
                continue
            key = [l.split(": ")[1] for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            b0, b1 = conn.recv(2)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", conn.recv(2))[0]
            mask = conn.recv(4)
            data = b""
            while len(data) < ln:
                data += conn.recv(ln - len(data))
            msg = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
            self.received.append(msg)
            out = json.dumps({"id": msg["id"], "error": {"message": "no"}} if self.reply_error else {"id": msg["id"], "result": {}}).encode()
            conn.sendall(bytes([0x81, len(out)]) + out)      # unmasked server frame

    def stop(self):
        self.srv.close()


class Holder(unittest.TestCase):
    def test_alive_and_set_download_behavior(self):
        fb = FakeBrowser()
        fb.start()
        try:
            base = "http://127.0.0.1:%d" % fb.port
            self.assertTrue(cdp.cdp_alive(base))
            ws = cdp.set_download_behavior(base, "C:\\Users\\u\\dl")
            ws.close()
            call = fb.received[0]
            self.assertEqual(call["method"], "Browser.setDownloadBehavior")
            self.assertEqual(call["params"]["behavior"], "allow")
            self.assertEqual(call["params"]["downloadPath"], "C:\\Users\\u\\dl")
        finally:
            fb.stop()

    def test_rejected_behavior_raises(self):
        fb = FakeBrowser(reply_error=True)
        fb.start()
        try:
            with self.assertRaises(cdp.CdpError):
                cdp.set_download_behavior("http://127.0.0.1:%d" % fb.port, "x")
        finally:
            fb.stop()

    def test_not_alive(self):
        self.assertFalse(cdp.cdp_alive("http://127.0.0.1:1"))


class CliArgs(unittest.TestCase):
    def test_argument_errors_exit_2(self):
        from chatgpt_backup.cli import main
        self.assertEqual(main(["run"]), 2)                                              # nothing chosen
        self.assertEqual(main(["run", "--all", "--from", "2026-01-01"]), 2)             # exclusive
        self.assertEqual(main(["run", "not-an-id"]), 2)
        self.assertEqual(main(["run", "--from", "garbage"]), 2)


if __name__ == "__main__":
    unittest.main()
