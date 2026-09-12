#!/usr/bin/env python3
"""RBXForge CLI - Phase 3C: local connection, tool layer, interactive AI REPL.

Runs a local WebSocket server on 127.0.0.1. The RBXForge Studio plugin (see
plugin/rbxforge.lua) connects to this process. This milestone implements:

- connection detection (the CLI reports when the plugin connects / disconnects)
- a hello/welcome handshake
- a test message: ping -> pong
- a tool registry (name, description, input schema) that validates arguments
  before sending a request; create_part is the first registered tool,
  create_script (Phase 6A) creates a Luau script, modify_instance (Phase 6B)
  modifies an allowlisted set of properties on an existing instance,
  inspect_hierarchy (Phase 4A) snapshots the Workspace instance tree,
  find_instances (Phase 4B) searches the live Workspace hierarchy by name,
  inspect_instance (Phase 4C) inspects one instance by its full path
  (request/response over the same socket), asset_search (Phase 7A) searches
  the Roblox Creator Store over the Open Cloud API -- a read-only local HTTP
  call that intentionally does NOT use the WebSocket/plugin path, and
  recommend_assets (Phase 7B) ranks those search results into a bounded,
  explainable recommendation list (also read-only, local, no WebSocket)
- an interactive AI REPL: ordinary text input is sent to the AI agent
  (cli/agent.py, Phase 4D), which drives a bounded multi-step loop - the model
  can call the inspection tools for project context, then an action tool such
  as create_part - all validated against the ToolRegistry and executed over the
  same protocol. A real prompt like "create a red cube" reaches Studio via
  create_part. Existing commands (ping / status / create_part / help / quit)
  still work, and 'ask' runs the agent explicitly.
- insert_asset (Phase 7C) inserts a Creator Store asset (by the exact id a
  prior asset_search / recommend_assets returned in this session) into the
  Studio project via the plugin, so the AI can search, rank, select, and
  place a real asset. The CLI refuses ids that never came from a search
  (the model can never invent one), validates the asset before anything is
  sent, and the plugin resolves the parent, places the asset (explicit
  position, near a referenced instance, or beside the SpawnLocation), and
  reports the resulting instance path.

Standard library only; no external dependencies.

Usage:
    rbxforge [--host HOST] [--port PORT]
    rbxforge --ping-once [--host HOST] [--port PORT] [--timeout SEC]
    rbxforge --create-part-once [--host HOST] [--port PORT] [--timeout SEC]
                                  [--request-timeout SEC]
    rbxforge --inspect-hierarchy-once [--host HOST] [--port PORT]
                  [--depth N] [--timeout SEC] [--request-timeout SEC]
    rbxforge --find-instances-once --query TEXT [--host HOST] [--port PORT]
                  [--max-results N] [--timeout SEC] [--request-timeout SEC]
    rbxforge --inspect-instance-once --path PATH [--host HOST] [--port PORT]
                  [--timeout SEC] [--request-timeout SEC]
    rbxforge --modify-instance-once --path PATH --properties JSON
                  [--host HOST] [--port PORT] [--timeout SEC] [--request-timeout SEC]
    rbxforge --asset-search-once --query TEXT [--asset-type TYPE] [--max-results N]
                  [--host HOST] [--port PORT] [--request-timeout SEC]
    rbxforge --recommend-assets-once --query TEXT [--asset-type TYPE] [--creator NAME]
                  [--max-results N] [--max-recommendations N]
                  [--host HOST] [--port PORT] [--request-timeout SEC]
    rbxforge --insert-asset-once --asset-id ID [--parent-path PATH]
                  [--position JSON] [--reference-path PATH]
                  [--host HOST] [--port PORT] [--timeout SEC] [--request-timeout SEC]

AI configuration comes from the environment (see cli/providers.py): set
RBXFORGE_PROVIDER/RBXFORGE_MODEL for the provider used by 'ask' and by plain
prompt input. The read-only asset_search tool (Phase 7A) searches the Roblox
Creator Store over the Open Cloud API and needs RBXFORGE_OPEN_CLOUD_API_KEY
(see cli/roblox_assets.py); recommend_assets (Phase 7B) ranks those results
into a bounded, explainable recommendation list (see cli/asset_ranking.py).
Both run locally and do not touch the plugin.

insert_asset (Phase 7C) goes over the WebSocket to the plugin and needs a
connected Studio session. It only accepts asset ids that a prior
asset_search / recommend_assets call in the same RBXForge session returned,
so the AI can never invent an id; --insert-asset-once is the explicit
human exception (the id is typed on the command line).

Protocol details: see docs/PROTOCOL.md.
"""

import argparse
import base64
import hashlib
import json
import re
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

# When this file runs as the CLI script the interpreter registers it only as
# "__main__". cli/agent.py imports "rbxforge" to reach the tool layer; without
# the alias below it would load a second copy and get distinct classes (its
# except-clauses would then never match this module's UnknownToolError /
# InvalidParamsError). Registering this module under "rbxforge" makes the agent
# use the same class objects.
# The manual importlib loader used by tests does not register this module in
# sys.modules, so only alias when the module is actually registered under its
# own name (real import / __main__ paths) and "rbxforge" is not taken.
if sys.modules.get("rbxforge") is None and sys.modules.get(__name__) is not None:
    sys.modules["rbxforge"] = sys.modules[__name__]

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
# Tool layer (see docs/TOOLS.md and docs/PROTOCOL.md)
# --------------------------------------------------------------------------- #


class ToolError(Exception):
    """Base class for tool-layer errors."""


class UnknownToolError(ToolError):
    """Raised when a tool name is not registered."""


class InvalidParamsError(ToolError):
    """Raised when params do not match a tool's input schema."""


def _validate_value(value, spec, path):
    """Validate one value against a schema fragment; returns an error string or None.

    The schema is a small JSON-like object with a ``type`` key. Supported types:
    ``object`` (with ``properties`` and ``required``), ``vec3`` (an object with
    numeric x, y, z), ``string`` (optionally ``min_length`` / ``enum``),
    ``number``, and ``boolean``.
    """
    kind = spec.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return path + " must be an object"
        if "min_properties" in spec and len(value) < spec["min_properties"]:
            return path + " must have at least {0} propert(y/ies)".format(
                spec["min_properties"])
        for required in spec.get("required", []):
            if required not in value:
                return path + " is missing required property '" + required + "'"
        declared = spec.get("properties", {})
        # ``additionalProperties: False`` rejects keys that are not in the
        # explicit allowlist (used by modify_instance's properties object so no
        # arbitrary property name supplied by the model reaches the plugin).
        if spec.get("additionalProperties") is False:
            for key in value:
                if key not in declared:
                    return path + " has an unsupported property '" + key + "'"
        for key, child in declared.items():
            if key in value:
                error = _validate_value(value[key], child, path + "." + key)
                if error is not None:
                    return error
    elif kind == "vec3":
        if not isinstance(value, dict):
            return path + " must be an object with numeric x, y, z"
        for axis in ("x", "y", "z"):
            component = value.get(axis)
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                return path + "." + axis + " must be a number"
    elif kind == "string":
        if not isinstance(value, str):
            return path + " must be a string"
        if "min_length" in spec and len(value) < spec["min_length"]:
            return path + " must be at least {0} character(s)".format(spec["min_length"])
        if "max_length" in spec and len(value) > spec["max_length"]:
            return path + " must be at most {0} character(s)".format(spec["max_length"])
        if "pattern" in spec and re.fullmatch(spec["pattern"], value) is None:
            return path + " must match the pattern " + spec["pattern"]
        if "enum" in spec and value not in spec["enum"]:
            choices = ", ".join(repr(choice) for choice in spec["enum"])
            return path + " must be one of " + choices
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return path + " must be a number"
        if spec.get("integer") and value != int(value):
            return path + " must be an integer"
        if "minimum" in spec and value < spec["minimum"]:
            return path + " must be at least {0}".format(spec["minimum"])
        if "maximum" in spec and value > spec["maximum"]:
            return path + " must be at most {0}".format(spec["maximum"])
    elif kind == "boolean":
        if not isinstance(value, bool):
            return path + " must be a boolean"
    else:
        return path + " uses an unsupported schema type: {0!r}".format(kind)
    return None


class Tool:
    """A single RBXForge operation: metadata plus parameter handling.

    ``input_schema`` is a small JSON-like object the CLI validates arguments
    against before any request is sent. ``run`` is the caller-side executor: a
    callable ``run(rbx, validated_params, timeout)`` that turns a validated call
    into the protocol's request/response exchange.
    """

    def __init__(self, name, description, input_schema, run):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._run = run

    def validate(self, params):
        """Validate ``params`` against the input schema; raise on failure."""
        error = _validate_value(params, self.input_schema, "params")
        if error is not None:
            raise InvalidParamsError(error)
        return params

    def run(self, rbx, params, timeout=10.0):
        return self._run(rbx, params, timeout)


class ToolRegistry:
    """Ordered collection of tools keyed by name."""

    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if tool.name in self._tools:
            raise ValueError("a tool named {0!r} is already registered".format(tool.name))
        self._tools[tool.name] = tool

    def get(self, name):
        return self._tools.get(name)

    def list(self):
        return [self._tools[name] for name in sorted(self._tools)]

    def execute(self, rbx, name, params, timeout=10.0):
        """Validate ``params`` for the named tool, then run it over the protocol."""
        tool = self.get(name)
        if tool is None:
            raise UnknownToolError("unknown tool: {0}".format(name))
        validated = tool.validate(params)
        return tool.run(rbx, validated, timeout)


CREATE_PART_COLORS = ["red", "blue", "green", "yellow", "white", "black", "gray"]

# Single source of truth for the create_part material enum (Phase 5C). The CLI
# validates this list first and the plugin validates it again; the model-facing
# schema inherits it automatically through the schema conversion.
CREATE_PART_MATERIALS = [
    "Plastic",
    "SmoothPlastic",
    "Neon",
    "Wood",
    "WoodPlanks",
    "Metal",
    "DiamondPlate",
    "Concrete",
    "Brick",
    "Glass",
    "Granite",
    "Marble",
    "Slate",
    "Sand",
    "Fabric",
    "Grass",
    "Ice",
]

CREATE_PART_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "min_length": 1},
        "position": {"type": "vec3"},
        "size": {"type": "vec3"},
        "color": {"type": "string", "enum": CREATE_PART_COLORS},
        # Phase 5B: optional physics flags. Omitted by the model/CLI, they fall
        # back to the defaults below (also applied by the plugin) so existing
        # create_part calls behave exactly as before.
        "anchored": {"type": "boolean", "default": True},
        "can_collide": {"type": "boolean", "default": True},
        # Phase 5C: optional material. Omitted it defaults to Plastic (also
        # applied independently by the plugin); case- and value-mismatches are
        # rejected by the CLI enum before send.
        "material": {"type": "string", "enum": CREATE_PART_MATERIALS, "default": "Plastic"},
    },
    "required": ["name", "position", "size", "color"],
}

CREATE_PART_DEFAULT_PARAMS = {
    "name": "RBXForgeTestPart",
    "position": {"x": 0, "y": 5, "z": 0},
    "size": {"x": 4, "y": 4, "z": 4},
    "color": "red",
    "anchored": True,
    "can_collide": True,
    "material": "Plastic",
}


def create_part_tool():
    """Build the create_part tool (the fixed-phase test parameters are the CLI
    defaults; the tool itself accepts any schema-valid parameters)."""

    def run(rbx, params, timeout):
        response = rbx.send_request("create_part", params, timeout)
        if response is None:
            rbx.log("create_part failed: no response from the plugin")
            return False
        if response.get("ok"):
            part = response.get("result") or {}
            rbx.log("create_part OK: created {0}".format(part.get("name")))
            return True
        error = response.get("error") or {}
        rbx.log("create_part FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "create_part",
        "Create a Part in workspace with the given name, position, size, and color.",
        CREATE_PART_SCHEMA,
        run,
    )


# Single source of truth for the create_script type enum (Phase 6). The CLI
# validates this list first and the plugin validates it again; the model-facing
# schema inherits it automatically through the schema conversion.
CREATE_SCRIPT_TYPES = ["Script", "LocalScript", "ModuleScript"]

CREATE_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "min_length": 1},
        "type": {"type": "string", "enum": CREATE_SCRIPT_TYPES, "default": "Script"},
        # Optional game-rooted parent path (e.g. "ServerScriptService.Scripts").
        # When omitted the plugin uses the per-type default container. The CLI
        # only checks it is a non-empty string; the plugin resolves it.
        "parent_path": {"type": "string", "min_length": 1},
        # Optional Luau source. Omitted (or empty) creates an empty script.
        "source": {"type": "string", "default": ""},
    },
    "required": ["name"],
}

CREATE_SCRIPT_DEFAULT_PARAMS = {
    "name": "RBXForgeTestScript",
    "type": "Script",
    "parent_path": "ServerScriptService",
    "source": 'print("Hello from RBXForge")\n',
}


def create_script_tool():
    """Build the create_script tool (Phase 6).

    Creates a Luau script (Script/LocalScript/ModuleScript) in the project via
    the plugin, placed at the given parent path (game-rooted) or the plugin's
    per-type default container when omitted. The CLI validates name/type/source
    first; the plugin re-validates and resolves the parent.
    """

    def run(rbx, params, timeout):
        response = rbx.send_request("create_script", params, timeout)
        if response is None:
            rbx.log("create_script failed: no response from the plugin")
            return False
        if response.get("ok"):
            result = response.get("result") or {}
            rbx.log("create_script OK: created {0} ({1}) at {2} ({3} chars)".format(
                result.get("name"), result.get("type"),
                result.get("path", "?"), result.get("source_length", "?")
            ))
            return True
        error = response.get("error") or {}
        rbx.log("create_script FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "create_script",
        "Create a Luau script (Script, LocalScript, or ModuleScript) in the "
        "project. parent_path is optional and game-rooted (e.g. "
        "'ServerScriptService.Scripts'); when omitted the plugin uses the "
        "per-type default container (ServerScriptService for Script, "
        "StarterPlayer.StarterPlayerScripts for LocalScript, ReplicatedStorage "
        "for ModuleScript). source is optional Luau source code.",
        CREATE_SCRIPT_SCHEMA,
        run,
    )


# Single source of truth for the modify_instance team_color enum (Phase 6B).
# These are valid Roblox BrickColor names, kept aligned with the 7 create_part
# colors so a model can reason about the palette in one place. The CLI validates
# this list first and the plugin validates it again.
MODIFY_TEAM_COLORS = [
    "Really red",
    "Bright blue",
    "Bright green",
    "Bright yellow",
    "White",
    "Black",
    "Medium stone grey",
]

# The modify_instance property allowlist (Phase 6B). BasePart properties apply
# to any BasePart (including SpawnLocation, which is a BasePart); SpawnLocation
# properties apply only to SpawnLocation instances. ``color`` reuses the
# create_part palette and ``material`` reuses the create_part material list. The
# nested ``properties`` object sets ``additionalProperties: False`` so any
# property name outside this allowlist is rejected by the CLI before send (the
# plugin independently re-validates the same allowlist).
MODIFY_INSTANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "min_length": 1},
        "properties": {
            "type": "object",
            "additionalProperties": False,
            "min_properties": 1,
            "properties": {
                "position": {"type": "vec3"},
                "size": {"type": "vec3"},
                "anchored": {"type": "boolean"},
                "can_collide": {"type": "boolean"},
                "transparency": {"type": "number", "minimum": 0, "maximum": 1},
                "color": {"type": "string", "enum": CREATE_PART_COLORS},
                "material": {"type": "string", "enum": CREATE_PART_MATERIALS},
                "enabled": {"type": "boolean"},
                "duration": {"type": "number", "minimum": 0},
                "neutral": {"type": "boolean"},
                "team_color": {"type": "string", "enum": MODIFY_TEAM_COLORS},
            },
        },
    },
    "required": ["path", "properties"],
}

# Fixed test parameters for --modify-instance-once and the REPL command. The
# default targets a SpawnLocation (every new place has one) and turns it into a
# neutral team-free spawn point.
MODIFY_INSTANCE_DEFAULT_PARAMS = {
    "path": "Workspace.SpawnLocation",
    "properties": {
        "neutral": True,
        "enabled": True,
    },
}


def modify_instance_tool():
    """Build the modify_instance tool (Phase 6B).

    Modifies an allowlisted set of properties on one existing instance in the
    project via the plugin, addressed by its full Workspace-rooted path. The CLI
    validates the path, the property names (unknown ones are rejected), and each
    value's type/range/enum before anything is sent; the plugin re-validates
    independently and applies the requested properties atomically.
    """

    def run(rbx, params, timeout):
        response = rbx.send_request("modify_instance", params, timeout)
        if response is None:
            rbx.log("modify_instance failed: no response from the plugin")
            return False
        if response.get("ok"):
            result = response.get("result") or {}
            rbx.log("modify_instance OK: {0} ({1}) changed: {2}".format(
                result.get("path", "?"),
                result.get("className", "?"),
                json.dumps(result.get("changed") or {}, sort_keys=True),
            ))
            return True
        error = response.get("error") or {}
        rbx.log("modify_instance FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "modify_instance",
        "Modify an allowlisted set of properties on one existing instance in "
        "the project, addressed by its full Workspace-rooted path (e.g. "
        "'Workspace.SpawnLocation'). Supported properties depend on the target "
        "class: BasePart accepts position/size (vec3 objects with numeric "
        "x/y/z), anchored/can_collide (booleans), transparency (number 0..1), "
        "color (string from the create_part palette), and material (string from "
        "the create_part material list); SpawnLocation additionally accepts "
        "enabled/neutral (booleans), duration (number >= 0), and team_color "
        "(string BrickColor name). Unknown property names and values outside "
        "the allowlists are rejected.",
        MODIFY_INSTANCE_SCHEMA,
        run,
    )


# Hierarchy snapshot schema. `depth` is optional (the CLI applies the default
# below when omitted); when given it must be a whole number in [1, 50] so the
# plugin response stays bounded.
DEFAULT_HIERARCHY_DEPTH = 3
MAX_HIERARCHY_DEPTH = 50

INSPECT_HIERARCHY_SCHEMA = {
    "type": "object",
    "properties": {
        "depth": {"type": "number", "integer": True, "minimum": 1, "maximum": MAX_HIERARCHY_DEPTH},
    },
    "required": [],
}


def inspect_hierarchy_tool():
    """Build the inspect_hierarchy tool (Phase 4A).

    Requests a snapshot of the Workspace instance tree from the plugin and logs
    a one-line summary (instance count, depth, truncation). The plugin returns a
    bounded tree of ``{name, className, children}`` nodes; the CLI keeps the
    default depth small so responses cannot balloon.
    """

    def run(rbx, params, timeout):
        request_params = dict(params)
        request_params.setdefault("depth", DEFAULT_HIERARCHY_DEPTH)
        response = rbx.send_request("inspect_hierarchy", request_params, timeout)
        if response is None:
            rbx.log("inspect_hierarchy failed: no response from the plugin")
            return False
        if response.get("ok"):
            result = response.get("result") or {}
            summary = "inspect_hierarchy OK: {0} instance(s) at depth {1}".format(
                result.get("count", "?"), result.get("depth", "?")
            )
            if result.get("truncated"):
                summary += " (truncated - children omitted beyond the depth limit)"
            rbx.log(summary)
            return True
        error = response.get("error") or {}
        rbx.log("inspect_hierarchy FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "inspect_hierarchy",
        "Return a snapshot of the Workspace instance tree (each instance's Name and "
        "ClassName) up to a maximum depth.",
        INSPECT_HIERARCHY_SCHEMA,
        run,
    )


# Instance search (Phase 4B). `query` is required and must be a non-empty
# string; `max_results` is optional (the CLI applies the default below when
# omitted) and must be a whole number in [1, 100] so the plugin response stays
# bounded even when the live hierarchy has many matches.
DEFAULT_FIND_MAX_RESULTS = 20
MAX_FIND_RESULTS = 100

FIND_INSTANCES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "min_length": 1},
        "max_results": {
            "type": "number", "integer": True, "minimum": 1, "maximum": MAX_FIND_RESULTS,
        },
    },
    "required": ["query"],
}


def find_instances_tool():
    """Build the find_instances tool (Phase 4B).

    Requests a case-insensitive name search over the live Workspace hierarchy
    from the plugin and logs a one-line summary (match count, truncation). The
    plugin returns a bounded list of ``{name, className, path}`` matches plus a
    total count; the CLI keeps the default max_results small so responses
    cannot balloon.
    """

    def run(rbx, params, timeout):
        request_params = dict(params)
        request_params.setdefault("max_results", DEFAULT_FIND_MAX_RESULTS)
        response = rbx.send_request("find_instances", request_params, timeout)
        if response is None:
            rbx.log("find_instances failed: no response from the plugin")
            return False
        if response.get("ok"):
            result = response.get("result") or {}
            summary = "find_instances OK: {0} match(es) for query {1!r}".format(
                result.get("total", "?"), result.get("query", "?")
            )
            if result.get("truncated"):
                summary += " (truncated - more matches exist beyond max_results)"
            rbx.log(summary)
            return True
        error = response.get("error") or {}
        rbx.log("find_instances FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "find_instances",
        "Search the live Workspace hierarchy for instances whose Name contains "
        "the query (case-insensitive) and return each match's Name, ClassName, "
        "and full Instance path.",
        FIND_INSTANCES_SCHEMA,
        run,
    )


# Instance inspection (Phase 4C). `path` is required and must be a non-empty
# string naming one instance inside Workspace, e.g. "Workspace.SpawnLocation"
# (dot or slash separators are accepted; the plugin enforces the full format
# and returns `not_found` when the path does not resolve).
INSPECT_INSTANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "min_length": 1},
    },
    "required": ["path"],
}


def inspect_instance_tool():
    """Build the inspect_instance tool (Phase 4C).

    Requests one live instance, addressed by its full path, and logs its name,
    className, full path, parent path, and the small allowlisted set of safe
    properties the plugin serialized. The plugin decides which properties (if
    any) an instance exposes; the CLI just renders what came back.
    """

    def run(rbx, params, timeout):
        response = rbx.send_request("inspect_instance", params, timeout)
        if response is None:
            rbx.log("inspect_instance failed: no response from the plugin")
            return False
        if response.get("ok"):
            result = response.get("result") or {}
            summary = "inspect_instance OK: {0} ({1}): {2}".format(
                result.get("path", "?"),
                result.get("className", "?"),
                json.dumps(result.get("properties") or {}),
            )
            rbx.log(summary)
            return True
        error = response.get("error") or {}
        rbx.log("inspect_instance FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "inspect_instance",
        "Inspect one instance in the live Workspace by its full path "
        "(e.g. 'Workspace.SpawnLocation'); returns its Name, ClassName, "
        "parent path, and a small allowlisted set of safe properties.",
        INSPECT_INSTANCE_SCHEMA,
        run,
    )


# Creator Store asset search (Phase 7A). `query` is required and must be a
# non-empty string; `asset_type` is an optional strict allowlist (the official
# searchCategoryType values); `max_results` is optional (the CLI applies the
# default below when omitted) and must be a whole number in [1, 20] so the API
# page stays bounded.
DEFAULT_ASSET_MAX_RESULTS = 5
MAX_ASSET_RESULTS = 20

# Phase 7B asset recommendation (cli/asset_ranking.py): the number of ranked
# recommendations returned per call stays bounded. ``limit`` on the tool is
# clamped to [1, MAX_ASSET_RECOMMENDATIONS]; when omitted it defaults to
# DEFAULT_ASSET_RECOMMENDATIONS. These agree with the constants in
# cli/asset_ranking.py (which owns the ranking logic).
DEFAULT_ASSET_RECOMMENDATIONS = 3
MAX_ASSET_RECOMMENDATIONS = 5


def recommend_assets_tool():
    """Build the recommend_assets tool (Phase 7B).

    Runs the Phase 7A read-only Creator Store search (cli/roblox_assets.py)
    for ``query`` and then ranks the returned metadata into a bounded,
    deterministic, explainable recommendation list (cli/asset_ranking.py).
    This is strictly READ-ONLY: ranking uses only the metadata already
    returned by the search, no additional API calls are made, and nothing is
    downloaded, inserted, or purchased. It runs locally like asset_search and
    deliberately does NOT go over the WebSocket/plugin protocol.

    Parameters mirror ``asset_search`` plus an optional ``creator`` bias and
    a ``limit`` on the number of recommendations returned. The result dict
    (truthy) is what the agent shows to the model as a bounded tool result;
    on failure the tool logs and returns False.
    """

    def run(rbx, params, timeout):
        ranking_mod = _import_ranking()
        if ranking_mod is None:
            rbx.log("recommend_assets FAILED: cli/asset_ranking.py could not be imported")
            return False
        assets_mod = _import_assets()
        if assets_mod is None:
            rbx.log("recommend_assets FAILED: cli/roblox_assets.py could not be imported")
            return False
        client = None
        resolver = getattr(rbx, "assets", None)
        if resolver is not None:
            try:
                client = resolver()
            except assets_mod.AssetConfigError as exc:
                rbx.log("recommend_assets FAILED: {0}".format(exc))
                return False
        if client is None:
            try:
                client = assets_mod.asset_client_from_env()
            except assets_mod.AssetConfigError as exc:
                rbx.log("recommend_assets FAILED: {0}".format(exc))
                return False
        query = params["query"]
        max_results = params.get("max_results") or DEFAULT_ASSET_MAX_RESULTS
        search_kwargs = {
            "query": query,
            "asset_type": params.get("asset_type"),
            "max_results": max_results,
            # Bound each HTTP request with the tool's own request timeout so
            # --request-timeout / execute_tool(timeout=...) really bounds the
            # call instead of silently falling back to the env-configured
            # client timeout (which defaults to 30s).
            "timeout": timeout,
        }
        try:
            result = client.search(**search_kwargs)
        except assets_mod.AssetConfigError as exc:
            rbx.log("recommend_assets FAILED: asset_search: {0}".format(exc))
            return False
        except assets_mod.AssetError as exc:
            rbx.log("recommend_assets FAILED: asset_search: {0}".format(exc))
            return False
        # Phase 7C: remember the search-result ids so insert_asset can accept
        # exactly an asset the model discovered (never an invented id).
        _remember_results(rbx, result.get("results") or [])
        try:
            ranked = ranking_mod.rank_assets(
                result.get("results") or [],
                query,
                asset_type=params.get("asset_type"),
                creator=params.get("creator"),
                limit=params.get("limit", DEFAULT_ASSET_RECOMMENDATIONS),
            )
        except ranking_mod.RankingError as exc:
            rbx.log("recommend_assets FAILED: {0}".format(exc))
            return False
        top = ", ".join(
            "#{0} {1} (score {2})".format(
                rec["rank"],
                rec["asset"].get("name") or rec["asset"].get("asset_id") or "?",
                rec["score"],
            )
            for rec in ranked["recommendations"]
        )
        summary = "recommend_assets OK: ranked {0} of {1} result(s) for query {2!r} - {3}".format(
            ranked["count"], ranked["evaluated"], ranked["query"], top or "no matches"
        )
        rbx.log(summary)
        return ranked

    module = _import_assets()
    if module is None:
        # The tool must still construct even when cli/roblox_assets.py cannot
        # be imported so the registry (and CLI startup) is not taken down.
        # ``run`` reports the failure gracefully; the ranking layer is even
        # more optional, so its import failure is deferred to ``run`` too.
        asset_types = [
            "Audio", "Model", "Decal", "Plugin", "MeshPart", "Video", "FontFamily",
        ]
    else:
        asset_types = list(module.ASSET_TYPES)
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "min_length": 1},
            "asset_type": {"type": "string", "enum": asset_types},
            "creator": {"type": "string", "min_length": 1},
            "max_results": {
                "type": "number", "integer": True,
                "minimum": 1, "maximum": MAX_ASSET_RESULTS,
            },
            "limit": {
                "type": "number", "integer": True,
                "minimum": 1, "maximum": MAX_ASSET_RECOMMENDATIONS,
            },
        },
        "required": ["query"],
    }
    return Tool(
        "recommend_assets",
        "Search the public Roblox Creator Store for assets matching a query "
        "(e.g. a player wants a 'shop model' or 'spooky audio') and return a "
        "bounded list of the best-ranked recommendations, each with a score "
        "and a human-readable reason (title/description term matches, asset "
        "type, creator, rating/usage metadata) so the caller can state why "
        "each asset was chosen. Always pairs with asset_search; ranking is "
        "deterministic and read-only - nothing is downloaded, inserted, or "
        "purchased. Accepts an optional 'creator' to bias toward a specific "
        "creator and an optional 'limit' (1..{0}) for the number of "
        "recommendations. Requires an Open Cloud API key set in "
        "RBXFORGE_OPEN_CLOUD_API_KEY.".format(MAX_ASSET_RECOMMENDATIONS),
        schema,
        run,
    )


# Phase 7C asset insertion into the Studio project. `insert_asset` only accepts
# an asset id that a prior asset_search / recommend_assets call returned in this
# session (RBXForge.remember_assets records them), so the AI can never invent an
# id. ``INSERTABLE_ASSET_TYPES`` is the CLI-side allowlist of Creator Store
# categories the plugin knows how to load and place into the project (Plugin and
# Video assets cannot be inserted into the Workspace this way).
INSERTABLE_ASSET_TYPES = frozenset({"Model", "MeshPart", "Decal", "Audio"})

#: Hard bound on how many asset ids are retained from search results so the
#: known-assets registry can never grow without bound.
MAX_KNOWN_ASSETS = 200

INSERT_ASSET_SCHEMA = {
    "type": "object",
    "properties": {
        # Digits-only string id, bounded in length so absurd ids are rejected
        # before anything is sent. The plugin re-validates and converts it.
        "asset_id": {
            "type": "string", "min_length": 1, "max_length": 16,
            "pattern": "^[0-9]+$",
        },
        # Optional game-rooted container path (e.g. "Workspace"). When omitted
        # the plugin places the asset directly under Workspace.
        "parent_path": {"type": "string", "min_length": 1},
        # Optional absolute placement. Mutually exclusive with reference_path:
        # give one or neither, never both.
        "position": {"type": "vec3"},
        # Optional full path of an existing instance to place the asset near
        # (e.g. "Workspace.SpawnLocation"); the plugin offsets from it.
        "reference_path": {"type": "string", "min_length": 1},
    },
    "required": ["asset_id"],
}


def insert_asset_tool():
    """Build the insert_asset tool (Phase 7C).

    Inserts a Creator Store asset into the currently connected Studio project
    via the plugin (request/response over the WebSocket, like create_part).
    The step before insertion is a completed asset_search / recommend_assets
    call; this tool refuses any asset id that was not among those results, so
    the model can never fabricate an id. The CLI validates the id format,
    the known-ids requirement, the insertable asset type, and the position /
    reference_path exclusivity before anything is sent; the plugin
    independently re-validates, loads the asset (InsertService:LoadAsset),
    resolves the parent, ensures a sibling-unique name, and positions the
    asset (explicit position, near the referenced instance, or beside the
    project's SpawnLocation). Returns clear success/error information.
    """

    def run(rbx, params, timeout):
        asset_id = params["asset_id"]
        # Phase 7C safety: the model may only insert an asset id that a prior
        # discover/rank step returned in this session. Connections that track
        # known assets (RBXForge) enforce it; test doubles without tracking
        # skip it, keeping every other tool's behavior unchanged.
        known = getattr(rbx, "known_asset", None)
        if known is not None:
            record = known(asset_id)
            if record is None:
                rbx.log(
                    "insert_asset REJECTED: asset_id {0!r} was not returned by a "
                    "prior asset_search / recommend_assets call - search first and "
                    "use an id exactly as it appears in the results".format(asset_id)
                )
                return False
            asset_type = record.get("asset_type")
            if asset_type is not None and asset_type not in INSERTABLE_ASSET_TYPES:
                rbx.log(
                    "insert_asset REJECTED: asset_id {0!r} is type {1!r}, which "
                    "cannot be inserted into the project (supported: {2})".format(
                        asset_id, asset_type,
                        ", ".join(sorted(INSERTABLE_ASSET_TYPES)),
                    )
                )
                return False
        if params.get("position") is not None and params.get("reference_path") is not None:
            rbx.log(
                "insert_asset REJECTED: provide either 'position' or "
                "'reference_path', not both"
            )
            return False
        response = rbx.send_request("insert_asset", params, timeout)
        if response is None:
            rbx.log("insert_asset failed: no response from the plugin")
            return False
        if response.get("ok"):
            result = response.get("result") or {}
            summary = "insert_asset OK: asset {0} inserted as {1} ({2}) at {3}".format(
                result.get("asset_id", "?"),
                result.get("name", "?"),
                result.get("class", "?"),
                result.get("path", "?"),
            )
            if result.get("positioned"):
                summary += " at {0}".format(json.dumps(result.get("position"), sort_keys=True))
            rbx.log(summary)
            return True
        error = response.get("error") or {}
        rbx.log("insert_asset FAILED: [{0}] {1}".format(
            error.get("code"), error.get("message")
        ))
        return False

    return Tool(
        "insert_asset",
        "Insert a Creator Store asset into the Studio project at a chosen or "
        "sensible location. The 'asset_id' MUST be the exact id of an asset "
        "already returned by asset_search or recommend_assets (ids are never "
        "invented); it is validated before anything is sent and the plugin "
        "loads it, gives it a unique name, and reports the resulting instance "
        "path. 'parent_path' (game-rooted, default Workspace) is the target "
        "container; supply exactly one of 'position' (absolute vec3) or "
        "reference_path (a path like 'Workspace.SpawnLocation' to place near) "
        "or neither (the plugin places the asset beside the project's "
        "SpawnLocation). Insertable asset types: Model, MeshPart, Decal, "
        "Audio. Read-only search never inserts; this tool does.",
        INSERT_ASSET_SCHEMA,
        run,
    )


def default_registry():
    """Build the registry with all built-in tools registered."""
    registry = ToolRegistry()
    registry.register(create_part_tool())
    registry.register(create_script_tool())
    registry.register(modify_instance_tool())
    registry.register(inspect_hierarchy_tool())
    registry.register(find_instances_tool())
    registry.register(inspect_instance_tool())
    registry.register(asset_search_tool())
    registry.register(recommend_assets_tool())
    registry.register(insert_asset_tool())
    return registry


def _remember_results(rbx, results):
    """Record search-result asset metadata on the connection (Phase 7C).

    ``insert_asset`` only accepts ids that a prior asset_search /
    recommend_assets call returned in the same session. The connection
    (RBXForge) stores a bounded ``{asset_id: record}`` table; test
    doubles without tracking simply skip this, which keeps every read-only
    tool's behavior unchanged.
    """
    remember = getattr(rbx, "remember_assets", None)
    if remember is not None:
        remember(results)


def asset_search_tool():
    """Build the asset_search tool (Phase 7A).

    Searches the public Roblox Creator Store via the official Open Cloud
    Creator Store API (cli/roblox_assets.py). This is a READ-ONLY local HTTP
    call from this process -- it deliberately does NOT go over the
    WebSocket/plugin protocol, and nothing is inserted, cloned, downloaded,
    or purchased. Configuration comes from the environment
    (RBXFORGE_OPEN_CLOUD_API_KEY; optional base URL / timeout), or from the
    connection's injected ``asset_client`` when one was provided. The returned
    structured result dict (truthy) is what the agent shows to the model as a
    bounded tool result; on failure the tool logs and returns False.
    """

    def run(rbx, params, timeout):
        assets_mod = _import_assets()
        if assets_mod is None:
            rbx.log("asset_search FAILED: cli/roblox_assets.py could not be imported")
            return False
        client = None
        resolver = getattr(rbx, "assets", None)
        if resolver is not None:
            try:
                client = resolver()
            except assets_mod.AssetConfigError as exc:
                rbx.log("asset_search FAILED: {0}".format(exc))
                return False
        if client is None:
            try:
                client = assets_mod.asset_client_from_env()
            except assets_mod.AssetConfigError as exc:
                rbx.log("asset_search FAILED: {0}".format(exc))
                return False
        max_results = params.get("max_results") or DEFAULT_ASSET_MAX_RESULTS
        search_kwargs = {
            "query": params["query"],
            "asset_type": params.get("asset_type"),
            "max_results": max_results,
            # Bound each HTTP request with the tool's own request timeout so
            # --request-timeout / execute_tool(timeout=...) really bounds the
            # call instead of silently falling back to the env-configured
            # client timeout (which defaults to 30s).
            "timeout": timeout,
        }
        try:
            result = client.search(**search_kwargs)
        except assets_mod.AssetConfigError as exc:
            rbx.log("asset_search FAILED: {0}".format(exc))
            return False
        except assets_mod.AssetError as exc:
            rbx.log("asset_search FAILED: {0}".format(exc))
            return False
        # Phase 7C: remember the returned ids so insert_asset can later accept
        # exactly these (never an invented id) and validate before sending.
        _remember_results(rbx, result.get("results") or [])
        summary = "asset_search OK: {0} result(s) for query {1!r}".format(
            len(result.get("results") or []), result.get("query")
        )
        if result.get("asset_type"):
            summary += " (asset_type={0})".format(result["asset_type"])
        if result.get("truncated"):
            summary += " (truncated - more results exist beyond max_results)"
        rbx.log(summary)
        return result

    module = _import_assets()
    if module is None:
        # The tool must still construct even when cli/roblox_assets.py cannot
        # be imported so the registry (and CLI startup) is not taken down.
        # ``run`` reports the failure gracefully; only the schema enum needs a
        # static copy of the official searchCategoryType allowlist.
        asset_types = [
            "Audio", "Model", "Decal", "Plugin", "MeshPart", "Video", "FontFamily",
        ]
    else:
        asset_types = list(module.ASSET_TYPES)
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "min_length": 1},
            "asset_type": {"type": "string", "enum": asset_types},
            "max_results": {
                "type": "number", "integer": True,
                "minimum": 1, "maximum": MAX_ASSET_RESULTS,
            },
        },
        "required": ["query"],
    }
    return Tool(
        "asset_search",
        "Search the public Roblox Creator Store for assets (models, decals, "
        "audio, plugins, meshes, videos, font families) matching a query and "
        "return a bounded list of matches with id, name, type, creator, "
        "description, and thumbnail URL. Read-only: nothing is inserted, "
        "cloned, downloaded, or purchased. Requires an Open Cloud API key set "
        "in RBXFORGE_OPEN_CLOUD_API_KEY.",
        schema,
        run,
    )


def _import_agent():
    """Lazily import cli/agent.py and return the module (or None).

    cli/ is not a package, so this mirrors agent.py's own sibling bootstrap:
    it works both when this file is run as a script (cli/ already on sys.path)
    and when it is loaded in-process by tests (cli/ not on sys.path). The import
    is lazy so plain CLI use never needs the agent/provider layers.
    """
    import importlib.util
    import os
    import sys

    try:
        import agent
        return agent
    except ImportError:
        here = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
        if here not in sys.path:
            sys.path.insert(0, here)
        try:
            import agent  # reload after cli/ was added to sys.path
            return agent
        except ImportError:
            return None


def _import_assets():
    """Lazily import cli/roblox_assets.py and return the module (or None).

    Mirrors ``_import_agent``: works both when this file runs as a script and
    when it is loaded in-process by tests. The import is lazy so plain CLI use
    never loads the asset-discovery layer unless an asset_search call happens.
    """
    import importlib.util
    import os
    import sys

    try:
        import roblox_assets
        return roblox_assets
    except ImportError:
        here = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
        if here not in sys.path:
            sys.path.insert(0, here)
        try:
            import roblox_assets  # reload after cli/ was added to sys.path
            return roblox_assets
        except ImportError:
            return None


def _import_ranking():
    """Lazily import cli/asset_ranking.py and return the module (or None).

    Phase 7B: mirrors ``_import_assets``. ``cli/asset_ranking.py`` imports
    ``cli/roblox_assets.py`` itself, so a successful ranking import also makes
    the asset layer importable; if either is missing, ``run`` reports the
    failure gracefully and the registry (and CLI startup) stays intact.
    """
    import importlib.util
    import os
    import sys

    try:
        import asset_ranking
        return asset_ranking
    except ImportError:
        here = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
        if here not in sys.path:
            sys.path.insert(0, here)
        try:
            import asset_ranking  # reload after cli/ was added to sys.path
            return asset_ranking
        except ImportError:
            return None


def _asset_client_from_env():
    """Build a RobloxAssetClient from the environment, or None on import failure."""
    module = _import_assets()
    if module is None:
        return None
    return module.asset_client_from_env()


# --------------------------------------------------------------------------- #
# RBXForge protocol layer (see docs/PROTOCOL.md)
# --------------------------------------------------------------------------- #


class RBXForge:
    """Connection tracking, message dispatch, and the ping command."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, console=None, registry=None,
                 asset_client=None):
        self.host = host
        self.port = port
        self.console = console if console is not None else REPLConsole()
        self.registry = registry if registry is not None else default_registry()
        self._asset_client = asset_client
        self.server = None
        self.connection = None
        self.connection_lock = threading.Lock()
        self.pong_events = {}
        self.pong_lock = threading.Lock()
        self.request_events = {}
        self.request_lock = threading.Lock()
        self._next_id = 0
        self._agent = None
        # Phase 7C: bounded table of asset ids returned by prior
        # asset_search / recommend_assets calls; insert_asset only accepts ids
        # recorded here, so the model can never invent one.
        self._known_assets = {}

    # -- logging ----------------------------------------------------------- #

    def log(self, message):
        self.console.log(message)

    def error(self, message):
        self.console.error(message)

    def assets(self):
        """Return the configured Roblox asset-search client (Phase 7A).

        Builds and caches one lazily from the environment
        (RBXFORGE_OPEN_CLOUD_API_KEY, see cli/roblox_assets.py) on first use,
        unless an ``asset_client`` was injected at construction. Raises the
        client's AssetConfigError when no API key is configured.
        """
        if self._asset_client is None:
            self._asset_client = _asset_client_from_env()
        return self._asset_client

    # -- Phase 7C: known-asset registry ------------------------------------ #

    def remember_assets(self, results):
        """Record asset ids from search results as known (bounded).

        Each non-dict / id-less entry is skipped; the first metadata snapshot
        for an id is kept. Kept hard-bounded: when the table would exceed
        :data:`MAX_KNOWN_ASSETS` entries the oldest records are dropped, so a
        long REPL/agent session can never grow the registry without bound
        (each record is small: name/asset_type/creator).
        """
        for entry in results or []:
            if not isinstance(entry, dict):
                continue
            asset_id = entry.get("asset_id")
            if asset_id is None:
                continue
            asset_id = str(asset_id).strip()
            if not asset_id:
                continue
            self._known_assets.setdefault(asset_id, {
                "name": entry.get("name"),
                "asset_type": entry.get("asset_type"),
                "creator": entry.get("creator"),
            })
            while len(self._known_assets) > MAX_KNOWN_ASSETS:
                self._known_assets.pop(next(iter(self._known_assets)))

    def asset_known(self, asset_id):
        """True when ``asset_id`` was returned by a prior search/ranking call."""
        return str(asset_id) in self._known_assets

    def known_asset(self, asset_id):
        """The recorded metadata snapshot for ``asset_id``, or None."""
        return self._known_assets.get(str(asset_id))

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
            self.log("✓ PONG received")
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

    def execute_tool(self, name, params, timeout=10.0):
        """Validate and run a registered tool; returns True/False and logs results."""
        try:
            return self.registry.execute(self, name, params, timeout)
        except UnknownToolError as exc:
            self.log("cannot execute: {0}".format(exc))
            return False
        except InvalidParamsError as exc:
            self.log("cannot execute {0}: invalid parameters: {1}".format(name, exc))
            return False

    def create_part(self, timeout=10.0):
        """Create the test part in Studio via the registered create_part tool."""
        return self.execute_tool("create_part", CREATE_PART_DEFAULT_PARAMS, timeout)

    def create_script(self, params=None, timeout=10.0):
        """Create a script in Studio via the registered create_script tool.

        ``params`` defaults to the fixed test parameters when omitted.
        """
        if params is None:
            params = CREATE_SCRIPT_DEFAULT_PARAMS
        return self.execute_tool("create_script", params, timeout)

    def modify_instance(self, path, properties, timeout=10.0):
        """Modify an instance's allowlisted properties in Studio via the
        registered modify_instance tool.

        ``path`` is the full Workspace-rooted path (e.g. "Workspace.SpawnLocation");
        ``properties`` is a dict of allowlisted property names to values.
        """
        return self.execute_tool(
            "modify_instance", {"path": path, "properties": properties}, timeout
        )

    def inspect_hierarchy(self, depth=None, timeout=10.0):
        """Snapshot the Studio Workspace hierarchy via the inspect_hierarchy tool.

        ``depth`` defaults to the plugin/CLI default (3) when omitted.
        """
        params = {}
        if depth is not None:
            params["depth"] = depth
        return self.execute_tool("inspect_hierarchy", params, timeout)

    def find_instances(self, query, max_results=None, timeout=10.0):
        """Search the Studio Workspace hierarchy via the find_instances tool.

        ``max_results`` defaults to the plugin/CLI default (20) when omitted.
        """
        params = {"query": query}
        if max_results is not None:
            params["max_results"] = max_results
        return self.execute_tool("find_instances", params, timeout)

    def inspect_instance(self, path, timeout=10.0):
        """Inspect one Studio Workspace instance via the inspect_instance tool.

        ``path`` names the instance, e.g. "Workspace.SpawnLocation".
        """
        return self.execute_tool("inspect_instance", {"path": path}, timeout)

    def asset_search(self, query, asset_type=None, max_results=None, timeout=10.0):
        """Search the Creator Store (Phase 7A) via the asset_search tool.

        Read-only: the search runs locally over the Open Cloud Creator Store
        API (see cli/roblox_assets.py) and never touches the plugin or the
        WebSocket protocol. Requires RBXFORGE_OPEN_CLOUD_API_KEY in the
        environment (or an injected ``asset_client``). Returns the bounded
        result dict on success (truthy), or False on failure.
        """
        params = {"query": query}
        if asset_type is not None:
            params["asset_type"] = asset_type
        if max_results is not None:
            params["max_results"] = max_results
        return self.execute_tool("asset_search", params, timeout)

    def recommend_assets(self, query, asset_type=None, creator=None,
                         max_results=None, limit=None, timeout=10.0):
        """Search the Creator Store and rank the results (Phase 7B).

        Runs the read-only Phase 7A search via ``asset_search`` and ranks the
        returned metadata into a bounded, deterministic, explainable list via
        the ``recommend_assets`` tool (see cli/asset_ranking.py). ``creator``
        optionally biases toward a specific creator; ``limit`` bounds the
        number of recommendations (clamped to 1..5, default 3). Read-only:
        no API calls beyond the search, and nothing is downloaded, inserted,
        or purchased. Returns the bounded result dict on success (truthy),
        or False on failure.
        """
        params = {"query": query}
        if asset_type is not None:
            params["asset_type"] = asset_type
        if creator is not None:
            params["creator"] = creator
        if max_results is not None:
            params["max_results"] = max_results
        if limit is not None:
            params["limit"] = limit
        return self.execute_tool("recommend_assets", params, timeout)

    def insert_asset(self, asset_id, parent_path=None, position=None,
                     reference_path=None, timeout=10.0):
        """Insert a Creator Store asset into the Studio project (Phase 7C).

        ``asset_id`` must be the exact id of an asset returned by a prior
        ``asset_search`` / ``recommend_assets`` call in this session (the tool
        rejects ids it has not seen, so ids are never invented). ``parent_path``
        is the optional game-rooted container (default Workspace); give at most
        one of ``position`` (absolute vec3) and ``reference_path`` (a path to
        place near, e.g. "Workspace.SpawnLocation"), or neither. Returns True
        when the plugin reports the insertion, else False.
        """
        params = {"asset_id": asset_id}
        if parent_path is not None:
            params["parent_path"] = parent_path
        if position is not None:
            params["position"] = position
        if reference_path is not None:
            params["reference_path"] = reference_path
        return self.execute_tool("insert_asset", params, timeout)

    def ask(self, prompt):
        """Run one natural-language prompt through the AI agent (Phase 3B-4D).

        The agent (cli/agent.py) uses the environment-configured provider, sends
        the registered tool definitions along with the prompt, and drives a
        bounded multi-step loop: it may call the inspection tools to gather
        project context, then an action tool such as create_part, all through
        this instance's ToolRegistry (this RBXForge acts as the connection
        handed to the tools). Single-step requests behave exactly as before.

        Provider errors, malformed output, unknown tools, invalid arguments,
        and execution failures are condensed into a short
        "[rbxforge] AI failed: ..." log line; nothing here raises, so the
        interactive REPL always survives an AI/agent failure.
        Returns True when the request completed (an action tool ran, or the
        model finished with a report), else False.
        """
        agent_mod = _import_agent()
        if agent_mod is None:
            self.log("AI agent unavailable: cli/agent.py could not be imported")
            return False
        if self._agent is None:
            try:
                self._agent = agent_mod.agent_from_env(registry=self.registry, rbx=self)
            except agent_mod.providers.ProviderError as exc:
                self.log("AI agent unavailable: {0}".format(exc))
                return False
        result = self._agent.run(prompt)
        if result.ok:
            if result.tool is not None:
                self.log("AI OK: called {0!r} -> {1!r}".format(result.tool.name, result.output))
            else:
                report = (result.message or "").strip()
                self.log("AI OK: {0}".format(report[:200] if report else "done"))
            return True
        code = (result.error or {}).get("code", "error")
        detail = (result.error or {}).get("message", "unknown error")
        self.log("AI failed: {0}: {1}".format(code, detail))
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
        elif command == "create_script":
            after = line.strip()[len(command):].strip()
            params = dict(CREATE_SCRIPT_DEFAULT_PARAMS) if after else None
            if params is not None:
                params["name"] = after
            rbx.create_script(params)
        elif command == "modify_instance":
            after = line.strip()[len(command):].strip()
            brace = after.find("{")
            if brace < 0:
                rbx.log("modify_instance: no JSON properties given (e.g. "
                        "'modify_instance Workspace.SpawnLocation {\"neutral\": false}')")
            else:
                path = after[:brace].strip()
                json_text = after[brace:]
                try:
                    properties = json.loads(json_text)
                except ValueError:
                    rbx.log("modify_instance: invalid JSON properties: {0!r}".format(json_text))
                else:
                    if not path:
                        rbx.log("modify_instance: no path given (e.g. "
                                "'modify_instance Workspace.SpawnLocation {\"neutral\": false}')")
                    elif not isinstance(properties, dict):
                        rbx.log("modify_instance: properties must be a JSON object")
                    else:
                        rbx.modify_instance(path, properties)
        elif command == "inspect_hierarchy":
            parts = line.split(None, 1)
            depth = None
            if len(parts) > 1:
                try:
                    depth = int(parts[1])
                except ValueError:
                    rbx.log("inspect_hierarchy: ignoring invalid depth {0!r}".format(parts[1]))
            rbx.inspect_hierarchy(depth)
        elif command == "find_instances":
            after = line.strip()[len(command):].strip()
            if not after:
                rbx.log("find_instances: no query given (e.g. 'find_instances Baseplate')")
            else:
                max_results = None
                words = after.split()
                if len(words) >= 2:
                    try:
                        parsed = int(words[-1])
                    except ValueError:
                        pass
                    else:
                        max_results = parsed
                        after = " ".join(words[:-1])
                rbx.find_instances(after, max_results)
        elif command == "inspect_instance":
            after = line.strip()[len(command):].strip()
            if not after:
                rbx.log("inspect_instance: no path given (e.g. "
                        "'inspect_instance Workspace.SpawnLocation')")
            else:
                rbx.inspect_instance(after)
        elif command == "asset_search":
            after = line.strip()[len(command):].strip()
            if not after:
                rbx.log("asset_search: no query given (e.g. 'asset_search sword')")
            else:
                max_results = None
                words = after.split()
                if len(words) >= 2:
                    try:
                        parsed = int(words[-1])
                    except ValueError:
                        pass
                    else:
                        max_results = parsed
                        after = " ".join(words[:-1])
                rbx.asset_search(after, max_results=max_results)
        elif command == "recommend_assets":
            after = line.strip()[len(command):].strip()
            if not after:
                rbx.log("recommend_assets: no query given (e.g. "
                        "'recommend_assets shop model')")
            else:
                limit = None
                words = after.split()
                if len(words) >= 2:
                    try:
                        parsed = int(words[-1])
                    except ValueError:
                        pass
                    else:
                        limit = parsed
                        after = " ".join(words[:-1])
                rbx.recommend_assets(after, limit=limit)
        elif command == "insert_asset":
            after = line.strip()[len(command):].strip()
            if not after:
                rbx.log("insert_asset: no asset_id given (e.g. "
                        "'insert_asset 135522 Workspace' - the id must come from an "
                        "earlier asset_search / recommend_assets, it is never invented)")
            else:
                words = after.split()
                asset_id = words[0]
                parent_path = words[1] if len(words) > 1 else None
                rbx.insert_asset(asset_id, parent_path=parent_path)
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
            print("  create_part - run the create_part tool (creates a test Part in Studio)")
            print("  create_script [name]")
            print("              - run the create_script tool (creates a test Script in")
            print("                ServerScriptService; optional name overrides the default)")
            print("  modify_instance <path> <json>")
            print("              - run the modify_instance tool (changes allowlisted")
            print("                properties on one instance, e.g.")
            print("                'modify_instance Workspace.SpawnLocation {\"neutral\": false}')")
            print("  inspect_hierarchy [depth]")
            print("              - snapshot the Workspace tree (default depth: 3)")
            print("  find_instances <query> [max_results]")
            print("              - search the Workspace for instances whose name matches")
            print("                <query>, case-insensitively (default max_results: 20)")
            print("  inspect_instance <path>")
            print("              - inspect one Workspace instance by its full path")
            print("                (e.g. Workspace.SpawnLocation)")
            print("  asset_search <query> [max_results]")
            print("              - search the Creator Store over the Open Cloud API")
            print("                (read-only; default max_results: 5)")
            print("  recommend_assets <query> [limit]")
            print("              - search the Creator Store and rank the results into")
            print("                a bounded, explainable recommendation list")
            print("                (read-only; default limit: 3, max: 5)")
            print("  insert_asset <asset_id> [parent_path]")
            print("              - insert a Creator Store asset into the project; the")
            print("                id must come from an earlier asset_search /")
            print("                recommend_assets (ids are never invented)")
            print("  status      - show connection status")
            print("  ask <text>  - send <text> to the AI agent (same as any other input)")
            print("  quit        - stop RBXForge")
            print("any other input is sent to the AI agent as a prompt")
        elif command == "ask":
            parts = line.split(None, 1)
            prompt = parts[1] if len(parts) > 1 else ""
            if not prompt:
                rbx.log("ask: no prompt given (e.g. 'ask create a red cube')")
            else:
                rbx.ask(prompt)
        else:
            rbx.ask(line)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rbxforge",
        description="RBXForge local process. Phase 3C: local WebSocket connection "
                    "with the RBXForge Studio plugin, a formal tool layer, and an "
                    "interactive AI REPL (plain text input goes to the AI agent).",
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
        "--create-script-once", action="store_true",
        help="wait for the plugin to connect, create one test script, report, then exit",
    )
    parser.add_argument(
        "--modify-instance-once", action="store_true",
        help="wait for the plugin to connect, modify the instance at --path with "
             "the --properties (JSON) allowlisted properties, report, then exit",
    )
    parser.add_argument(
        "--inspect-hierarchy-once", action="store_true",
        help="wait for the plugin to connect, snapshot the Workspace hierarchy, "
             "report, then exit",
    )
    parser.add_argument(
        "--find-instances-once", action="store_true",
        help="wait for the plugin to connect, search the Workspace hierarchy for "
             "--query, report, then exit",
    )
    parser.add_argument(
        "--inspect-instance-once", action="store_true",
        help="wait for the plugin to connect, inspect the Workspace instance at "
             "--path, report, then exit",
    )
    parser.add_argument(
        "--asset-search-once", action="store_true",
        help="search the Creator Store for --query over the Open Cloud API "
             "(read-only, local HTTP, no plugin needed), report, then exit",
    )
    parser.add_argument(
        "--recommend-assets-once", action="store_true",
        help="search the Creator Store for --query, rank the results, and "
             "return a bounded list of ranked recommendations (read-only, "
             "local HTTP, no plugin needed), report, then exit",
    )
    parser.add_argument(
        "--insert-asset-once", action="store_true",
        help="wait for the plugin to connect, insert the Creator Store asset "
             "--asset-id into the project, report, then exit (the agent path "
             "instead requires the id to come from a prior asset_search / "
             "recommend_assets call)",
    )
    parser.add_argument(
        "--depth", type=int, default=None,
        help="maximum hierarchy depth for --inspect-hierarchy-once (default: 3; "
             "must be a whole number in 1..{0})".format(MAX_HIERARCHY_DEPTH),
    )
    parser.add_argument(
        "--query", default=None,
        help="instance name query for --find-instances-once, or Creator Store "
             "query for --asset-search-once / --recommend-assets-once",
    )
    parser.add_argument(
        "--path", default=None,
        help="full instance path for --inspect-instance-once, e.g. "
             "'Workspace.SpawnLocation'",
    )
    parser.add_argument(
        "--properties", default=None,
        help="JSON object of allowlisted properties for --modify-instance-once, "
             "e.g. '{\"neutral\": false, \"duration\": 3}'",
    )
    parser.add_argument(
        "--max-results", type=int, default=None,
        help="maximum matches for --find-instances-once (default: {0}; must be a "
             "whole number in 1..{1}) or --asset-search-once (default: 5; must "
             "be a whole number in 1..20)".format(DEFAULT_FIND_MAX_RESULTS, MAX_FIND_RESULTS),
    )
    parser.add_argument(
        "--max-recommendations", type=int, default=None,
        help="maximum ranked recommendations for --recommend-assets-once "
             "(default: {0}; must be a whole number in 1..{1})".format(
                 DEFAULT_ASSET_RECOMMENDATIONS, MAX_ASSET_RECOMMENDATIONS),
    )
    ASSET_SEARCH_CLI_ASSET_TYPES = [
        "Audio", "Model", "Decal", "Plugin", "MeshPart", "Video", "FontFamily",
    ]
    parser.add_argument(
        "--asset-type", default=None, choices=ASSET_SEARCH_CLI_ASSET_TYPES,
        help="optional Creator Store category filter for --asset-search-once / "
             "--recommend-assets-once, one of Audio, Model, Decal, Plugin, "
             "MeshPart, Video, FontFamily",
    )
    parser.add_argument(
        "--creator", default=None,
        help="optional creator-name bias for --recommend-assets-once "
             "(ranking favors assets whose creator matches)",
    )
    parser.add_argument(
        "--asset-id", default=None,
        help="Creator Store asset id for --insert-asset-once (digits only; "
             "the agent path never invents ids)",
    )
    parser.add_argument(
        "--parent-path", default=None,
        help="game-rooted container path for --insert-asset-once "
             "(default: Workspace)",
    )
    parser.add_argument(
        "--position", default=None,
        help='absolute 3D position JSON for --insert-asset-once, e.g. '
             '{"x": 0, "y": 5, "z": 0} (mutually exclusive with '
             '--reference-path)',
    )
    parser.add_argument(
        "--reference-path", default=None,
        help="full path of an existing instance to place the inserted asset "
             "near for --insert-asset-once (e.g. Workspace.SpawnLocation; "
             "mutually exclusive with --position)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0,
        help="seconds to wait for the plugin in the --*-once modes "
             "(default: 30)",
    )
    parser.add_argument(
        "--request-timeout", type=float, default=10.0,
        help="seconds to wait for a tool response (default: 10)",
    )
    args = parser.parse_args(argv)

    console = REPLConsole()
    rbx = RBXForge(args.host, args.port, console=console)

    if args.asset_search_once:
        # Phase 7A --asset-search-once is a local HTTP call over the Open Cloud
        # API: it deliberately does not start the WebSocket server, and there is
        # nothing to wait for plugin-wise. Validate the required flag first,
        # then run the search and exit.
        if not args.query:
            rbx.error("--asset-search-once requires --query <text>")
            return 2
        rbx.log("tools registered: {0}".format(
            ", ".join(tool.name for tool in rbx.registry.list())
        ))
        return 0 if rbx.asset_search(
            args.query, asset_type=args.asset_type,
            max_results=args.max_results, timeout=args.request_timeout,
        ) else 4

    if args.recommend_assets_once:
        # Phase 7B --recommend-assets-once mirrors --asset-search-once: local
        # HTTP only, no WebSocket server, nothing to wait for plugin-wise.
        if not args.query:
            rbx.error("--recommend-assets-once requires --query <text>")
            return 2
        rbx.log("tools registered: {0}".format(
            ", ".join(tool.name for tool in rbx.registry.list())
        ))
        return 0 if rbx.recommend_assets(
            args.query, asset_type=args.asset_type, creator=args.creator,
            max_results=args.max_results, limit=args.max_recommendations,
            timeout=args.request_timeout,
        ) else 4

    try:
        address = rbx.start()
    except OSError as exc:
        rbx.error("could not start server on {0}:{1}: {2}".format(args.host, args.port, exc))
        return 1
    rbx.log("listening on ws://{0}:{1} (protocol v{2})".format(address[0], address[1], PROTOCOL_VERSION))
    rbx.log("load the RBXForge plugin in Roblox Studio and click 'Connect'.")
    rbx.log("tools registered: {0}".format(
        ", ".join(tool.name for tool in rbx.registry.list())
    ))

    try:
        if args.ping_once:
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.send_ping() else 3
        if args.create_part_once:
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.create_part(args.request_timeout) else 4
        if args.create_script_once:
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.create_script(timeout=args.request_timeout) else 4
        if args.modify_instance_once:
            if not args.path:
                rbx.error("--modify-instance-once requires --path <path>")
                return 2
            if not args.properties:
                rbx.error("--modify-instance-once requires --properties <json>")
                return 2
            try:
                properties = json.loads(args.properties)
            except ValueError:
                rbx.error("--properties must be a valid JSON object")
                return 2
            if not isinstance(properties, dict):
                rbx.error("--properties must be a JSON object")
                return 2
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.modify_instance(args.path, properties, args.request_timeout) else 4
        if args.inspect_hierarchy_once:
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.inspect_hierarchy(args.depth, args.request_timeout) else 4
        if args.find_instances_once:
            if not args.query:
                rbx.error("--find-instances-once requires --query <text>")
                return 2
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.find_instances(args.query, args.max_results, args.request_timeout) else 4
        if args.inspect_instance_once:
            if not args.path:
                rbx.error("--inspect-instance-once requires --path <text>")
                return 2
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            return 0 if rbx.inspect_instance(args.path, args.request_timeout) else 4
        if args.insert_asset_once:
            # Phase 7C --insert-asset-once: the human types the id explicitly,
            # so it is recorded as a known asset before execution (the Agent
            # path instead enforces that ids come from search results).
            if not args.asset_id:
                rbx.error("--insert-asset-once requires --asset-id <id>")
                return 2
            if args.position and args.reference_path:
                rbx.error("--position and --reference-path may not both be given")
                return 2
            position = None
            if args.position:
                try:
                    position = json.loads(args.position)
                except ValueError:
                    rbx.error('--position must be valid JSON, e.g. {"x": 0, "y": 5, "z": 0}')
                    return 2
                if not isinstance(position, dict):
                    rbx.error("--position must be a JSON object with numeric x, y, z")
                    return 2
            if not wait_for_plugin(rbx, args.timeout):
                return 2
            rbx.remember_assets([{"asset_id": args.asset_id}])
            rbx.log(
                "insert_asset: accepting --asset-id {0!r} provided directly on "
                "the command line (the Agent path requires ids from "
                "asset_search / recommend_assets)".format(args.asset_id)
            )
            return 0 if rbx.insert_asset(
                args.asset_id, parent_path=args.parent_path, position=position,
                reference_path=args.reference_path, timeout=args.request_timeout,
            ) else 4
        repl(rbx, console)
    finally:
        rbx.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
