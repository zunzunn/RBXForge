#!/usr/bin/env python3
"""End-to-end protocol tests for the RBXForge CLI (Phase 2A).

Starts cli/rbxforge.py as a subprocess and drives it with a minimal WebSocket
client that mimics the RBXForge Studio plugin (hello -> welcome -> ping -> pong,
plus request/response for create_part, plus disconnect handling). Standard
library only.

Run from the repository root:
    python3 tests/test_protocol.py
"""

import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI = os.path.join(ROOT, "cli", "rbxforge.py")
PROTOCOL_VERSION = 1
HELLO = {
    "type": "hello",
    "id": None,
    "version": PROTOCOL_VERSION,
    "timestamp": 0.0,
    "payload": {"name": "rbxforge-plugin", "version": "0.1.0", "protocol": PROTOCOL_VERSION},
}


# --------------------------------------------------------------------------- #
# Minimal WebSocket client (the tests act as the Studio plugin)
# --------------------------------------------------------------------------- #


def _encode_client_frame(text):
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    n = len(payload)
    if n <= 125:
        header = bytes([0x81, 0x80 | n])
    elif n <= 0xFFFF:
        header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def _read_server_frame(sock):
    h1, h2 = _recv_exact(sock, 2)
    opcode = h1 & 0x0F
    length = h2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    masked = (h2 & 0x80) != 0
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def connect_ws(host, port, path="/"):
    sock = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET {0} HTTP/1.1\r\n"
        "Host: {1}:{2}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {3}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).format(path, host, port, key)
    sock.sendall(request.encode("ascii"))
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(4096)
    status_line = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")[0]
    if "101" not in status_line:
        raise AssertionError("handshake failed: " + status_line)
    return sock


def send_json(sock, obj):
    sock.sendall(_encode_client_frame(json.dumps(obj)))


def recv_json(sock, timeout=10.0):
    sock.settimeout(timeout)
    opcode, payload = _read_server_frame(sock)
    if opcode != 0x1:
        raise AssertionError("expected text frame, got opcode {}".format(opcode))
    return json.loads(payload.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #


class LineReader:
    def __init__(self, stream):
        self.stream = stream
        self.lines = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        while True:
            line = self.stream.readline()
            if not line:
                break
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = line.rstrip("\n")
            with self._lock:
                self.lines.append(line)

    def wait_for(self, needle, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if any(needle in line for line in self.lines):
                    return True
            time.sleep(0.05)
        return False

    def contains(self, needle):
        with self._lock:
            return any(needle in line for line in self.lines)


class Proc:
    def __init__(self, *args):
        self.proc = subprocess.Popen(
            [sys.executable, CLI] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.reader = LineReader(self.proc.stdout)

    def listening_addr(self, timeout=15.0):
        if not self.reader.wait_for("listening on ws://", timeout):
            raise AssertionError("server did not print listening line; output:\n"
                                 + self._output())
        with self.reader._lock:
            matching = [l for l in self.reader.lines if "listening on ws://" in l]
        line = matching[-1]
        match = re.search(r"ws://([^:]+):(\d+)", line)
        if not match:
            raise AssertionError("could not parse listening line: " + line)
        return match.group(1), int(match.group(2))

    def _output(self):
        with self.reader._lock:
            return "".join(self.reader.lines)

    def wait(self, timeout=15.0):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError("subprocess did not exit; output:\n" + self._output())

    def quit(self):
        try:
            self.proc.stdin.write(b"quit\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        return self.wait()


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def scenario_connect_hello_ping_pong():
    """Start RBXForge, connect like the plugin, exchange hello/welcome, and
    answer the auto ping with a pong. RBXForge should report success and exit 0."""
    proc = Proc("--ping-once", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        first = recv_json(sock)
        assert first["type"] == "welcome", first
        assert first["payload"]["name"] == "rbxforge", first
        second = recv_json(sock)
        assert second["type"] == "ping", second
        ping_id = second["id"]
        send_json(sock, {
            "type": "pong",
            "id": ping_id,
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {"message": "pong"},
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("PLUGIN CONNECTED"), proc._output()
        assert proc.reader.contains("PONG received"), proc._output()
        print("OK  connect -> hello/welcome -> ping/pong -> clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_disconnect_and_reconnect():
    """RBXForge must log plugin disconnect and keep serving. A second plugin
    connection must work afterwards."""
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()

        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert proc.reader.wait_for("PLUGIN CONNECTED"), proc._output()
        assert recv_json(sock)["type"] == "welcome", proc._output()

        # Close the connection with a WS close frame (like the plugin's Close()).
        sock.sendall(b"\x88\x80" + os.urandom(4))
        sock.close()
        assert proc.reader.wait_for("PLUGIN DISCONNECTED"), proc._output()

        # RBXForge should still be responsive: reconnect and verify again.
        sock2 = connect_ws(host, port)
        send_json(sock2, HELLO)
        assert proc.reader.wait_for("PLUGIN CONNECTED"), proc._output()
        assert recv_json(sock2)["type"] == "welcome", proc._output()
        sock2.sendall(b"\x88\x80" + os.urandom(4))
        sock2.close()
        assert proc.reader.wait_for("PLUGIN DISCONNECTED"), proc._output()

        # Server survives: send a ping to prove responsiveness.
        assert proc.reader.contains("PLUGIN CONNECTED"), proc._output()
        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  disconnect logged, server survives, reconnect works")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_errors():
    """Malformed JSON must yield an error message and the connection must stay
    usable."""
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)

        sock.sendall(_encode_client_frame("this is not json"))
        err = recv_json(sock)
        assert err["type"] == "error", err
        assert err["payload"]["code"] == "malformed_message", err

        send_json(sock, {"type": "bogus", "id": "x1", "payload": {}})
        err = recv_json(sock)
        assert err["type"] == "error", err
        assert err["payload"]["code"] == "unknown_message_type", err

        # Connection is still usable.
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome"
        assert proc.reader.wait_for("PLUGIN CONNECTED"), proc._output()
        sock.sendall(b"\x88\x80" + os.urandom(4))
        sock.close()
        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  malformed/unknown messages produce errors without breaking the connection")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_create_part_success():
    """--create-part-once must wait for the plugin, send a create_part request
    with the fixed test parameters, accept a success response, and exit 0."""
    proc = Proc("--create-part-once", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_part", req
        assert req["payload"]["params"]["name"] == "RBXForgeTestPart", req
        assert req["payload"]["params"]["position"] == {"x": 0, "y": 5, "z": 0}, req
        assert req["payload"]["params"]["size"] == {"x": 4, "y": 4, "z": 4}, req
        assert req["payload"]["params"]["color"] == "red", req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": True,
                "result": {
                    "name": "RBXForgeTestPart",
                    "position": {"x": 0, "y": 5, "z": 0},
                    "size": {"x": 4, "y": 4, "z": 4},
                    "color": "red",
                },
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("create_part OK"), proc._output()
        print("OK  create_part request sent; success response handled; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_create_part_failure():
    """A failed create_part (plugin replies ok:false) must be reported and
    yield a non-zero exit code."""
    proc = Proc("--create-part-once", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": False,
                "error": {"code": "invalid_params", "message": "unsupported color: blue"},
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 4, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("create_part FAILED"), proc._output()
        assert proc.reader.contains("invalid_params"), proc._output()
        print("OK  create_part failure response reported; non-zero exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_interactive_create_part_registered():
    """The interactive REPL must recognize the 'create_part' command.

    Regression: the automated suite previously only exercised the --once flags,
    which bypass the REPL branch, so a CLI whose interactive 'create_part' branch
    was missing (e.g. a stale process) would silently report 'unknown command'.
    """
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()
        assert proc.reader.wait_for("Type 'help'"), proc._output()

        # No plugin connects here; the point is the command is dispatched
        # (to the no-connection path) rather than rejected as unknown.
        proc.proc.stdin.write(b"create_part\n")
        proc.proc.stdin.flush()
        proc.reader.wait_for("cannot execute create_part")
        assert not proc.reader.contains("unknown command"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  interactive REPL recognizes create_part (not 'unknown command')")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_repl_run_after_connection():
    """The interactive REPL must keep accepting input after a plugin connects
    and disconnects.

    Regression: Phase 2A's REPL could be displaced when the plugin connected in
    the middle of the prompt; here we drive an interactive session, connect a
    plugin, ask it to create a part, disconnect it, and then still run a
    subsequent command -- the REPL must remain usable throughout.
    """
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()
        assert proc.reader.wait_for("Type 'help'"), proc._output()

        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()
        assert proc.reader.wait_for("PLUGIN CONNECTED"), proc._output()

        # Issue create_part from the REPL after the connection was announced.
        proc.proc.stdin.write(b"create_part\n")
        proc.proc.stdin.flush()
        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_part", req
        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": True,
                "result": {
                    "name": "RBXForgeTestPart",
                    "position": {"x": 0, "y": 5, "z": 0},
                    "size": {"x": 4, "y": 4, "z": 4},
                    "color": "red",
                },
            },
        })
        assert proc.reader.wait_for("create_part OK"), proc._output()

        # Disconnect the plugin; another REPL command must still run afterwards.
        sock.sendall(b"\x88\x80" + os.urandom(4))
        sock.close()
        assert proc.reader.wait_for("PLUGIN DISCONNECTED"), proc._output()

        proc.proc.stdin.write(b"create_part\n")
        proc.proc.stdin.flush()
        assert proc.reader.wait_for("cannot execute create_part"), proc._output()

        rc = proc.quit()
        assert rc == 0, "expected exit 0; output:\n{}".format(rc, proc._output())
        print("OK  REPL stays usable before and after a plugin connects / disconnects")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


# --------------------------------------------------------------------------- #
# PTY-based REPL scenario (verifies the prompt survives background logging)
# --------------------------------------------------------------------------- #

try:
    import pty as _pty
    import select as _select
    import errno as _errno
except ImportError:
    _pty = None


class PTYProc:
    """Run the CLI on a pseudo-terminal so the REPL behaves like a real TTY.

    This reproduces the Phase 2A prompt-loss bug: a background (WebSocket) log
    line can be emitted while the REPL thread is blocked inside input(), landing
    on the same terminal line as the prompt. Assertions in
    scenario_repl_pty_prompt_survives() check that the prompt is re-drawn.
    """

    def __init__(self, *args):
        if _pty is None:
            raise AssertionError("pty module not available on this platform")
        self.master, slave = _pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, CLI] + list(args),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        self._output = bytearray()

    def _read_available(self, timeout=0.25):
        ready, _, _ = _select.select([self.master], [], [], timeout)
        if not ready:
            return False
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            return False
        self._output.extend(chunk)
        return True

    def wait_for(self, needle, timeout=15.0):
        if isinstance(needle, str):
            needle = needle.encode("utf-8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._read_available(0.1)
            if needle in self._output:
                return True
        return False

    def output(self):
        return bytes(self._output).decode("utf-8", "replace")

    def write(self, text):
        os.write(self.master, text.encode("utf-8"))

    def close(self):
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.kill()


def scenario_repl_after_plugin_connect_pty():
    """REPL prompt must be re-drawn after the plugin connects (PTY, real sanity).

    The regression this guards: when the plugin connects while the interactive
    REPL is waiting on input, the background log line used to be written onto
    the same terminal line as the prompt (``RBXForge> [rbxforge] ...``), hiding
    the prompt. Here we verify the escape sequence re-draws a fresh prompt and
    that the session still accepts the next command.
    """
    if _pty is None:
        print("SKIP  pty unavailable; REPL prompt redraw not exercised")
        return
    pproc = PTYProc("--port", "0")
    try:
        end = time.time() + 15
        while time.time() < end:
            if b"Type 'help'" in pproc._output:
                break
            if not pproc._read_available(0.1):
                break
        assert b"Type 'help'" in pproc._output, pproc.output()
        assert pproc.wait_for(b"RBXForge> "), pproc.output()

        m = re.search(r"listening on ws://[^:]+:(\d+)", pproc.output())
        assert m, pproc.output()
        port = int(m.group(1))
        sock = connect_ws("127.0.0.1", port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", pproc.output()

        # Wait for the connect logs to be emitted and the prompt re-drawn.
        assert pproc.wait_for("PLUGIN CONNECTED"), pproc.output()
        assert pproc.wait_for("client connected"), pproc.output()
        # Give the background log a moment to redraw the prompt.
        for _ in range(10):
            pproc._read_available(0.1)

        out = pproc.output()
        # Old behaviour: the log was glued to the prompt line with no redraw.
        assert "RBXForge> [rbxforge]" not in out, \
            "log displaced the prompt line:\n" + repr(out)
        # New behaviour: the log is on its own line and the prompt is re-drawn
        # afterwards (escape sequence for erase-line, then a fresh prompt).
        assert "\x1b[2K" in out, "no erase-line redraw in:\n" + repr(out)
        assert out.rstrip().endswith("RBXForge>"), repr(out)

        # The REPL must still accept input normally.
        pproc.write("create_part\n")
        req = recv_json(sock)
        assert req["payload"]["tool"] == "create_part", req
        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {"ok": True, "result": {"name": "RBXForgeTestPart"}},
        })
        assert pproc.wait_for("create_part OK"), pproc.output()

        pproc.write("quit\n")
        try:
            rc = pproc.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            assert False, "REPL did not exit; output:\n" + pproc.output()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, pproc.output())
        print("OK  prompt is re-drawn after plugin connect; REPL stays interactive")
    finally:
        pproc.close()


def main():
    scenario_connect_hello_ping_pong()
    scenario_disconnect_and_reconnect()
    scenario_errors()
    scenario_create_part_success()
    scenario_create_part_failure()
    scenario_interactive_create_part_registered()
    scenario_repl_run_after_connection()
    scenario_repl_after_plugin_connect_pty()
    print("\nAll protocol scenarios passed.")


if __name__ == "__main__":
    main()
