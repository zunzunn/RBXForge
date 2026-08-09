#!/usr/bin/env python3
"""RBXForge CLI - Phase 2A: local connection plus one Studio operation.

Runs a local WebSocket server on 127.0.0.1. The RBXForge Studio plugin (see
plugin/rbxforge.lua) connects to this process. This milestone implements:

- connection detection (the CLI reports when the plugin connects / disconnects)
- a hello/welcome handshake
- a test message: ping -> pong
- one Studio operation: create_part (request/response over the same socket)

Standard library only; no external dependencies.

Usage:
    rbxforge [--host HOST] [--port PORT]
    rbxforge --ping-once [--host HOST] [--port PORT] [--timeout SEC]
    rbxforge --create-part-once [--host HOST] [--port PORT] [--timeout SEC]
                                  [--request-timeout SEC]

Protocol details: see docs/PROTOCOL.md.
"""

import argparse
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time

APP_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7676
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RECV_TIMEOUT = 5.0

# --------------------------------------------------------------------------- #
# Minimal RFC 6455 WebSocket framing. Only text frames plus the control frames
# (close / ping / pong) needed for a clean connection are handled.
# --------------------------------------------------------------------------- #


def _ws_encode_frame(payload: bytes, opcode: int) -> bytes:
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n <= 125:
        header += bytes([n])
    elif n <= 0xFFFF:
        header += struct.pack(">BH", 126, n)
    else:
        header += struct.pack(">BQ", 127, n)
    return header + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def server_handshake(sock: socket.socket) -> dict:
    """Perform the server side of the WebSocket opening handshake."""
    sock.settimeout(10.0)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed during handshake")
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        raise ConnectionError("malformed request line")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    if headers.get("upgrade", "").lower() != "websocket":
        raise ConnectionError("not a WebSocket upgrade request")
    key = headers.get("sec-websocket-key")
    if not key:
        raise ConnectionError("missing Sec-WebSocket-Key header")
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: " + accept + "\r\n"
        "\r\n"
    )
    sock.sendall(response.encode("ascii"))
    return {
        "method": parts[0],
        "path": parts[1],
        "version": parts[2],
        "headers": headers,
    }


def read_frame(sock: socket.socket):
    """Read one frame; returns (opcode, unmasked_payload_bytes)."""
    header = _recv_exact(sock, 2)
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    masked = (b2 & 0x80) != 0
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class WSConnection:
    """A single established WebSocket connection on the server side."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.sock.settimeout(RECV_TIMEOUT)
        self.closed = False
        self.name = None   # set when the plugin sends hello
        self.version = None
        self.protocol = None

    def send_text(self, text):
        if self.closed:
            return False
        try:
            self.sock.sendall(_ws_encode_frame(text.encode("utf-8"), 0x1))
            return True
        except OSError:
            self.close()
            return False

    def send_json(self, obj):
        return self.send_text(json.dumps(obj))

    def send_close(self):
        try:
            self.sock.sendall(_ws_encode_frame(b"", 0x8))
        except OSError:
            pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class WSServer:
    """Threaded local-only WebSocket server with callback hooks."""

    def __init__(self, host, port, on_open=None, on_message=None, on_close=None):
        self.host = host
        self.port = port
        self.on_open = on_open
        self.on_message = on_message
        self.on_close = on_close
        self.sock = None
        self.connections = []
        self.lock = threading.Lock()
        self._stopped = threading.Event()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(4)
        self.sock.settimeout(1.0)
        address = self.sock.getsockname()
        self.port = address[1]
        self._thread = threading.Thread(
            target=self._accept_loop, name="ws-accept", daemon=True
        )
        self._thread.start()
        return address

    def _accept_loop(self):
        while not self._stopped.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn, addr), name="ws-conn", daemon=True
            ).start()

    def _handle(self, conn, addr):
        try:
            server_handshake(conn)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass
            return
        client = WSConnection(conn, addr)
        with self.lock:
            self.connections.append(client)
        try:
            if self.on_open:
                self.on_open(client)
            self._read_loop(client)
        finally:
            client.close()
            with self.lock:
                if client in self.connections:
                    self.connections.remove(client)
            if self.on_close:
                self.on_close(client)

    def _read_loop(self, client):
        while not self._stopped.is_set() and not client.closed:
            try:
                opcode, payload = read_frame(client.sock)
            except socket.timeout:
                continue
            except (OSError, ConnectionError):
                break
            if opcode == 0x8:  # close
                client.send_close()
                break
            elif opcode == 0x9:  # ping
                try:
                    client.sock.sendall(_ws_encode_frame(payload, 0xA))
                except OSError:
                    break
            elif opcode == 0xA:  # pong
                continue
            elif opcode == 0x1:  # text
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if self.on_message:
                    self.on_message(client, text)
            elif opcode == 0x2:  # binary - unsupported in this milestone
                if self.on_message:
                    self.on_message(client, None)
                continue
            else:
                break

    def stop(self):
        self._stopped.set()
        with self.lock:
            clients = list(self.connections)
        for client in clients:
            client.close()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Terminal output coordination
# --------------------------------------------------------------------------- #


class REPLConsole:
    """Serialize stdout writes so the interactive prompt survives background I/O.

    WebSocket events (connect / disconnect / messages) are logged from daemon
    threads while the REPL thread may be waiting on ``input()``. A plain
    ``print()`` from a background thread then lands in the middle of the prompt
    line (e.g. ``RBXForge> [rbxforge] PLUGIN CONNECTED``), displacing the prompt
    and making interactive input unreliable.

    This console makes prompt drawing and message writing one critical section:
    when a message must be written while a prompt is visible, it first moves the
    cursor back to the start of the line, erases the line, writes the message,
    and finally re-draws the prompt, so the prompt stays present and usable.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._prompt = None

    def set_prompt(self, prompt_text):
        """Remember the active prompt for re-drawing; None disables it."""
        self._prompt = prompt_text

    def draw_prompt(self):
        """Print the prompt. Blocking input is expected after this call."""
        with self._lock:
            if self._prompt is not None:
                sys.stdout.write(self._prompt)
                sys.stdout.flush()

    def _write(self, message):
        if self._prompt is None:
            sys.stdout.write(message)
            return
        # Move to column 0, erase the current line, write the message, then
        # re-draw the prompt so it is not lost behind the message.
        sys.stdout.write("\r\x1b[2K" + message + self._prompt)

    def log(self, message):
        with self._lock:
            self._write("[rbxforge] " + message + "\n")
            sys.stdout.flush()

    def error(self, message):
        with self._lock:
            if self._prompt is None:
                sys.stderr.write("[rbxforge] error: " + message + "\n")
            else:
                # Mirror the log behaviour: clear the prompt line on stderr too
                # (the terminal still renders it over the prompt).
                sys.stderr.write("\r\x1b[2K[rbxforge] error: " + message + "\n")
                sys.stdout.write(self._prompt)
                sys.stdout.flush()
            sys.stderr.flush()


# --------------------------------------------------------------------------- #
# RBXForge protocol layer (see docs/PROTOCOL.md)
# --------------------------------------------------------------------------- #


class RBXForge:
    """Connection tracking, message dispatch, and the ping command."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, console=None):
        self.host = host
        self.port = port
        self.console = console if console is not None else REPLConsole()
        self.server = None
        self.connection = None
        self.connection_lock = threading.Lock()
        self.pong_events = {}
        self.pong_lock = threading.Lock()
        self.request_events = {}
        self.request_lock = threading.Lock()
        self._next_id = 0

    # -- logging ----------------------------------------------------------- #

    def log(self, message):
        self.console.log(message)

    def error(self, message):
        self.console.error(message)

    # -- server callbacks -------------------------------------------------- #

    def on_open(self, client):
        self.log(
            "client connected from {0}:{1} (waiting for hello)".format(
                client.addr[0], client.addr[1]
            )
        )

    def on_message(self, client, text):
        if text is None:
            self.log("received a binary message (unsupported in this milestone); ignoring")
            return
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self.log("received a non-JSON message; sending error")
            self._send_error(client, None, "malformed_message", "message is not valid JSON")
            return
        if not isinstance(message, dict):
            self._send_error(client, None, "malformed_message", "message must be a JSON object")
            return
        mtype = message.get("type")
        mid = message.get("id")
        if mtype == "hello":
            self._on_hello(client, message)
        elif mtype == "pong":
            self._on_pong(client, message)
        elif mtype == "response":
            self._on_response(client, message)
        elif mtype == "bye":
            self.log("plugin sent bye")
        elif mtype == "error":
            payload = message.get("payload") or {}
            self.log(
                "plugin reported error: [{0}] {1}".format(
                    payload.get("code"), payload.get("message")
                )
            )
        else:
            self._send_error(client, mid, "unknown_message_type",
                             "unknown message type: {0!r}".format(mtype))

    def _on_hello(self, client, message):
        payload = message.get("payload") or {}
        with self.connection_lock:
            if self.connection is not None and self.connection is not client:
                self.log("a plugin is already connected; dropping the previous connection")
                self.connection.close()
            self.connection = client
        client.name = payload.get("name") or "rbxforge-plugin"
        client.version = payload.get("version")
        client.protocol = payload.get("protocol")
        self.log(
            "PLUGIN CONNECTED: {0} (version={1} protocol={2}) from {3}:{4}".format(
                client.name,
                client.version if client.version is not None else "?",
                client.protocol if client.protocol is not None else "?",
                client.addr[0],
                client.addr[1],
            )
        )
        self._send(client, "welcome", payload={
            "name": "rbxforge",
            "version": APP_VERSION,
            "protocol": PROTOCOL_VERSION,
        })

    def _on_pong(self, client, message):
        mid = message.get("id")
        with self.pong_lock:
            event = self.pong_events.get(mid) if mid is not None else None
        if event is not None:
            event["received_at"] = time.monotonic()
            event["event"].set()
            self.log("received pong (id={0})".format(mid))
        else:
            self.log("received unexpected pong (id={0}); ignoring".format(mid))

    def _on_response(self, client, message):
        mid = message.get("id")
        with self.request_lock:
            event = self.request_events.get(mid) if mid is not None else None
        if event is not None:
            event["received_at"] = time.monotonic()
            event["response"] = message.get("payload") or {}
            event["event"].set()
            self.log("received response (id={0})".format(mid))
        else:
            self.log("received unexpected response (id={0}); ignoring".format(mid))

    def on_close(self, client):
        was_plugin = client.name is not None
        with self.connection_lock:
            if self.connection is client:
                self.connection = None
        if was_plugin:
            self.log("PLUGIN DISCONNECTED: {0}".format(client.name))
        else:
            self.log("client disconnected")

    # -- outbound messages ------------------------------------------------- #

    def _send(self, client, mtype, mid=None, payload=None):
        message = {
            "type": mtype,
            "id": mid,
            "version": PROTOCOL_VERSION,
            "timestamp": time.time(),
            "payload": payload or {},
        }
        return client.send_json(message)

    def _send_error(self, client, mid, code, message):
        self._send(client, "error", mid=mid, payload={"code": code, "message": message})

    def send_ping(self, timeout=10.0):
        with self.connection_lock:
            client = self.connection
        if client is None:
            self.log(
                "cannot ping: no plugin is connected "
                "(start RBXForge, then click Connect in the Studio plugin)"
            )
            return False
        with self.pong_lock:
            self._next_id += 1
            mid = "ping-{0}".format(self._next_id)
            event = {"event": threading.Event(), "received_at": None}
            self.pong_events[mid] = event
        sent = self._send(client, "ping", mid=mid, payload={
            "message": "ping",
            "timestamp": time.time(),
        })
        if not sent:
            with self.pong_lock:
                self.pong_events.pop(mid, None)
            self.log("cannot ping: failed to send (plugin disconnected?)")
            return False
        started = time.monotonic()
        event["event"].wait(timeout)
        with self.pong_lock:
            self.pong_events.pop(mid, None)
        if event["received_at"] is not None:
            rtt = (event["received_at"] - started) * 1000.0
            self.log("PONG received for {0} in {1:.1f} ms".format(mid, rtt))
            return True
        self.log("timed out waiting for pong ({0}) after {1:g}s".format(mid, timeout))
        return False

    def send_request(self, tool, params, timeout=10.0):
        """Send a tool request and wait for its response.

        Returns the response payload on success, or None on timeout / send
        failure / no plugin connection.
        """
        with self.connection_lock:
            client = self.connection
        if client is None:
            self.log(
                "cannot execute {0}: no plugin is connected "
                "(start RBXForge, then click Connect in the Studio plugin)".format(tool)
            )
            return None
        with self.request_lock:
            self._next_id += 1
            mid = "req-{0}".format(self._next_id)
            event = {"event": threading.Event(), "received_at": None, "response": None}
            self.request_events[mid] = event
        sent = self._send(client, "request", mid=mid, payload={
            "tool": tool,
            "params": params,
        })
        if not sent:
            with self.request_lock:
                self.request_events.pop(mid, None)
            self.log("cannot execute {0}: failed to send (plugin disconnected?)".format(tool))
            return None
        started = time.monotonic()
        event["event"].wait(timeout)
        with self.request_lock:
            self.request_events.pop(mid, None)
        if event["received_at"] is not None:
            elapsed = (event["received_at"] - started) * 1000.0
            self.log("response received for {0} in {1:.1f} ms".format(mid, elapsed))
            return event["response"]
        self.log("timed out waiting for response ({0}) after {1:g}s".format(mid, timeout))
        return None

    def create_part(self, timeout=10.0):
        """Create a test part in Studio via the plugin (Phase 2A)."""
        params = {
            "name": "RBXForgeTestPart",
            "position": {"x": 0, "y": 5, "z": 0},
            "size": {"x": 4, "y": 4, "z": 4},
            "color": "red",
        }
        response = self.send_request("create_part", params, timeout)
        if response is None:
            self.log("create_part failed: no response from the plugin")
            return False
        if response.get("ok"):
            part = response.get("result") or {}
            self.log("create_part OK: created {0}".format(part.get("name")))
            return True
        error = response.get("error") or {}
        self.log("create_part FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    # -- lifecycle --------------------------------------------------------- #

    def start(self):
        self.server = WSServer(
            self.host,
            self.port,
            on_open=self.on_open,
            on_message=self.on_message,
            on_close=self.on_close,
        )
        return self.server.start()

    def stop(self):
        if self.server:
            self.server.stop()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def wait_for_plugin(rbx, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with rbx.connection_lock:
            if rbx.connection is not None:
                return True
        time.sleep(0.1)
    rbx.log("timed out waiting for the plugin to connect")
    return False


def repl(rbx, console, prompt="RBXForge> "):
    print("Type 'help' for commands.")
    while True:
        console.set_prompt(prompt)
        console.draw_prompt()
        try:
            line = input()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            return
        finally:
            console.set_prompt(None)
        line = line.strip()
        if not line:
            continue
        command = line.split()[0].lower()
        if command in ("quit", "exit", "q"):
            return
        elif command == "ping":
            rbx.send_ping()
        elif command == "create_part":
            rbx.create_part()
        elif command == "status":
            with rbx.connection_lock:
                client = rbx.connection
            if client is not None:
                rbx.log(
                    "connected: {0} (version={1}) at {2}:{3}".format(
                        client.name,
                        client.version if client.version is not None else "?",
                        client.addr[0],
                        client.addr[1],
                    )
                )
            else:
                rbx.log("no plugin connected")
        elif command == "help":
            print("commands:")
            print("  ping        - send a ping to the connected plugin and wait for pong")
            print("  create_part - create a test Part in Studio via the plugin")
            print("  status      - show connection status")
            print("  quit        - stop RBXForge")
        else:
            print("unknown command: {0} (try 'help')".format(command))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rbxforge",
        description="RBXForge local process. Phase 1: local WebSocket connection "
                    "with the RBXForge Studio plugin.",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help="host to bind (default: {0})".format(DEFAULT_HOST),
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="port to bind (default: {0}; 0 picks a free port)".format(DEFAULT_PORT),
    )
    parser.add_argument(
        "--ping-once", action="store_true",
        help="wait for the plugin to connect, send one ping, report, then exit",
    )
    parser.add_argument(
        "--create-part-once", action="store_true",
        help="wait for the plugin to connect, create one test part, report, then exit",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0,
        help="seconds to wait for the plugin in --ping-once/--create-part-once mode "
             "(default: 30)",
    )
    parser.add_argument(
        "--request-timeout", type=float, default=10.0,
        help="seconds to wait for a tool response (default: 10)",
    )
    args = parser.parse_args(argv)

    console = REPLConsole()
    rbx = RBXForge(args.host, args.port, console=console)
    try:
        address = rbx.start()
    except OSError as exc:
        rbx.error("could not start server on {0}:{1}: {2}".format(args.host, args.port, exc))
        return 1
    rbx.log("listening on ws://{0}:{1} (protocol v{2})".format(address[0], address[1], PROTOCOL_VERSION))
    rbx.log("load the RBXForge plugin in Roblox Studio and click 'Connect'.")

    try:
        if args.ping_once:
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.send_ping() else 3
        if args.create_part_once:
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.create_part(args.request_timeout) else 4
        repl(rbx, console)
    finally:
        rbx.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
