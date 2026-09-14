#!/usr/bin/env python3
"""End-to-end protocol + tool-layer tests for the RBXForge CLI (Phase 2B).

Starts cli/rbxforge.py as a subprocess and drives it with a minimal WebSocket
client that mimics the RBXForge Studio plugin (hello -> welcome -> ping -> pong,
plus request/response for create_part, plus disconnect handling). Standard
library only.

The tool-layer tests import cli/rbxforge.py in-process to verify the registry
registers create_part with metadata and rejects invalid arguments before any
request is sent.

Run from the repository root:
    python3 tests/test_protocol.py
"""

import base64
import importlib.util
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

# The CLI module is imported in-process for the tool-layer tests.
_CLI_MODULE = None


def load_cli_module():
    global _CLI_MODULE
    if _CLI_MODULE is None:
        spec = importlib.util.spec_from_file_location("rbxforge_cli_mod", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLI_MODULE = module
    return _CLI_MODULE


class FakeRBX:
    """Stand-in for the RBXForge connection given to tools in-process.

    ``response_payload`` is what ``send_request`` returns (the plugin's reply).
    Every request is recorded so tests can prove what was actually sent.
    ``known_assets`` pre-seeds the Phase 7C known-assets registry the way a
    prior asset_search / recommend_assets call would (``{asset_id: record}``);
    with the default empty table insert_asset rejects every id before sending,
    which is exactly the invented-id protection.
    """

    def __init__(self, response_payload=None, known_assets=None):
        self.response_payload = response_payload
        self.requests = []
        self.logs = []
        self.known_assets = dict(known_assets) if known_assets else {}

    def send_request(self, tool, params, timeout):
        self.requests.append((tool, params))
        return self.response_payload

    def log(self, message):
        self.logs.append(message)

    def remember_assets(self, results):
        count = 0
        for entry in results or []:
            if not isinstance(entry, dict):
                continue
            asset_id = entry.get("asset_id")
            if asset_id is None:
                continue
            asset_id = str(asset_id).strip()
            if asset_id:
                self.known_assets.setdefault(asset_id, {
                    "name": entry.get("name"),
                    "asset_type": entry.get("asset_type"),
                    "creator": entry.get("creator"),
                })
                count += 1
        return count

    def asset_known(self, asset_id):
        return str(asset_id) in self.known_assets

    def known_asset(self, asset_id):
        return self.known_assets.get(str(asset_id))


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
# Phase 4A: inspect_hierarchy scenarios
# --------------------------------------------------------------------------- #


def hierarchy_response(count=2, depth=2, truncated=False):
    return {
        "ok": True,
        "result": {
            "root": "Workspace",
            "depth": depth,
            "count": count,
            "truncated": truncated,
            "tree": [
                {
                    "name": "Workspace",
                    "className": "Workspace",
                    "children": [
                        {"name": "Baseplate", "className": "Part", "children": []},
                    ],
                }
            ],
        },
    }


def scenario_inspect_hierarchy_mock_response():
    """inspect_hierarchy must be executable through the ToolRegistry and accept
    a mock (in-process) hierarchy response, reporting it in a log summary."""
    mod = load_cli_module()
    rbx = FakeRBX(hierarchy_response(count=2, depth=2))
    registry = mod.default_registry()
    result = registry.execute(rbx, "inspect_hierarchy", {"depth": 2}, timeout=5.0)
    assert result is True, result
    assert rbx.requests == [("inspect_hierarchy", {"depth": 2})], rbx.requests
    assert any("inspect_hierarchy OK: 2 instance(s) at depth 2" in line for line in rbx.logs), \
        rbx.logs
    print("OK  inspect_hierarchy executes through the ToolRegistry with a mock response")


def scenario_inspect_hierarchy_depth_semantics():
    """Depth is optional (default 3) and passed through exactly."""
    mod = load_cli_module()
    registry = mod.default_registry()

    rbx = FakeRBX(hierarchy_response(count=3, depth=3))
    assert registry.execute(rbx, "inspect_hierarchy", {}, timeout=5.0) is True
    assert rbx.requests == [("inspect_hierarchy", {"depth": 3})], rbx.requests

    rbx = FakeRBX(hierarchy_response(count=5, depth=1))
    assert registry.execute(rbx, "inspect_hierarchy", {"depth": 1}, timeout=5.0) is True
    assert rbx.requests == [("inspect_hierarchy", {"depth": 1})], rbx.requests

    # A truncated response is called out in the summary.
    rbx = FakeRBX(hierarchy_response(count=5, depth=1, truncated=True))
    assert registry.execute(rbx, "inspect_hierarchy", {"depth": 1}, timeout=5.0) is True
    assert any("(truncated" in line for line in rbx.logs), rbx.logs
    print("OK  depth defaults to 3, is passed through, and truncation is reported")


def scenario_inspect_hierarchy_empty_hierarchy():
    """A leaf-only Workspace (no children) is a valid, empty hierarchy."""
    mod = load_cli_module()
    rbx = FakeRBX(hierarchy_response(count=1, depth=3))
    registry = mod.default_registry()
    result = registry.execute(rbx, "inspect_hierarchy", {}, timeout=5.0)
    assert result is True, result
    assert rbx.requests == [("inspect_hierarchy", {"depth": 3})], rbx.requests
    assert any("inspect_hierarchy OK: 1 instance(s) at depth 3" in line for line in rbx.logs), \
        rbx.logs
    print("OK  empty (leaf-only) hierarchy handled as a success")


def scenario_inspect_hierarchy_invalid_depth():
    """Depth must be a whole number in [1, MAX] or the call is rejected with no
    request sent (validation is not weakened)."""
    mod = load_cli_module()
    tool = mod.default_registry().get("inspect_hierarchy")
    assert tool is not None

    good = [None, 1, 2, mod.DEFAULT_HIERARCHY_DEPTH, mod.MAX_HIERARCHY_DEPTH]
    for depth in good:
        params = {} if depth is None else {"depth": depth}
        tool.validate(params)  # must not raise

    bad = [
        (0, "depth must be at least 1"),
        (-3, "depth must be at least 1"),
        (mod.MAX_HIERARCHY_DEPTH + 1, "depth must be at most"),
        (2.5, "depth must be an integer"),
        ("3", "depth must be a number"),
    ]
    for depth, fragment in bad:
        try:
            tool.validate({"depth": depth})
        except mod.InvalidParamsError as exc:
            assert fragment in str(exc), (depth, exc)
        else:
            raise AssertionError("inspect_hierarchy accepted invalid depth: {0!r}".format(depth))

    # execute rejects invalid depth up front: a validation error is raised and
    # nothing is sent (the same contract as the other tools).
    rbx = FakeRBX(hierarchy_response())
    registry = mod.default_registry()
    for depth in (0, -3, 2.5):
        try:
            registry.execute(rbx, "inspect_hierarchy", {"depth": depth}, timeout=5.0)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("execute accepted invalid depth: {0!r}".format(depth))
    assert rbx.requests == [], rbx.requests
    print("OK  invalid hierarchy depth rejected before sending (0/-1/>max/float/string)")


def scenario_inspect_hierarchy_roundtrip():
    """--inspect-hierarchy-once must wait for the plugin, send an inspect_hierarchy
    request with the given depth, accept a bounded tree response, and exit 0."""
    proc = Proc("--inspect-hierarchy-once", "--depth", "2", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "inspect_hierarchy", req
        assert req["payload"]["params"] == {"depth": 2}, req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": True,
                "result": {
                    "root": "Workspace",
                    "depth": 2,
                    "count": 2,
                    "truncated": False,
                    "tree": [
                        {
                            "name": "Workspace",
                            "className": "Workspace",
                            "children": [
                                {"name": "Baseplate", "className": "Part", "children": []},
                            ],
                        }
                    ],
                },
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("inspect_hierarchy OK: 2 instance(s) at depth 2"), \
            proc._output()
        print("OK  inspect_hierarchy round-trip through the protocol; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


# --------------------------------------------------------------------------- #
# Phase 4B: find_instances scenarios
# --------------------------------------------------------------------------- #


def find_instances_response(total=1, count=None, truncated=None, query="Baseplate",
                            max_results=20, matches=None):
    """A canned find_instances success result. ``count`` and ``truncated``
    default to consistent values for the given ``total`` / ``matches``."""
    if matches is None:
        matches = [
            {"name": "Match {0}".format(i), "className": "Part",
             "path": "Workspace/Match {0}".format(i)}
            for i in range(total if count is None else count)
        ]
    count = count if count is not None else len(matches)
    truncated = truncated if truncated is not None else (total > count)
    return {
        "ok": True,
        "result": {
            "query": query,
            "max_results": max_results,
            "total": total,
            "count": count,
            "truncated": truncated,
            "matches": matches,
        },
    }


def scenario_find_instances_mock_response():
    """find_instances must run through the ToolRegistry, send the query with the
    default max_results (20), and report the match count in a log summary."""
    mod = load_cli_module()
    rbx = FakeRBX(find_instances_response(total=2, count=2))
    registry = mod.default_registry()
    result = registry.execute(rbx, "find_instances", {"query": "Baseplate"}, timeout=5.0)
    assert result is True, result
    assert rbx.requests == [("find_instances", {"query": "Baseplate", "max_results": 20})], \
        rbx.requests
    assert any("find_instances OK: 2 match(es) for query 'Baseplate'" in line
               for line in rbx.logs), rbx.logs
    print("OK  find_instances executes through the ToolRegistry with a mock response")


def scenario_find_instances_max_results_and_truncation():
    """max_results is optional (default 20) and passed through exactly; a
    truncated response is called out, and a zero-match result is a success."""
    mod = load_cli_module()
    registry = mod.default_registry()

    rbx = FakeRBX(find_instances_response(total=3, count=2, truncated=True, max_results=2))
    assert registry.execute(rbx, "find_instances", {"query": "shop", "max_results": 2},
                            timeout=5.0) is True
    assert rbx.requests == [("find_instances", {"query": "shop", "max_results": 2})], \
        rbx.requests
    assert any("(truncated" in line for line in rbx.logs), rbx.logs

    rbx = FakeRBX(find_instances_response(total=0, count=0, truncated=False, query="zzz_none"))
    assert registry.execute(rbx, "find_instances", {"query": "zzz_none"}, timeout=5.0) is True
    assert rbx.requests == [("find_instances", {"query": "zzz_none", "max_results": 20})], \
        rbx.requests
    assert any("find_instances OK: 0 match(es) for query 'zzz_none'" in line
               for line in rbx.logs), rbx.logs
    print("OK  max_results passed through; truncation and no-match results reported")


def scenario_find_instances_validation():
    """query must be a non-empty string and max_results must be a whole number in
    [1, MAX], or the call is rejected with no request sent."""
    mod = load_cli_module()
    tool = mod.default_registry().get("find_instances")
    assert tool is not None

    good = [
        {"query": "a"},
        {"query": "Baseplate", "max_results": 1},
        {"query": "Shop Door", "max_results": mod.MAX_FIND_RESULTS},
    ]
    for params in good:
        tool.validate(params)  # must not raise

    bad = [
        ("missing query", {}),
        ("empty query", {"query": ""}),
        ("query not a string", {"query": 42}),
        ("query not a string (list)", {"query": ["a"]}),
        ("max_results 0", {"query": "a", "max_results": 0}),
        ("max_results negative", {"query": "a", "max_results": -3}),
        ("max_results too high", {"query": "a", "max_results": mod.MAX_FIND_RESULTS + 1}),
        ("max_results float", {"query": "a", "max_results": 2.5}),
        ("max_results string", {"query": "a", "max_results": "5"}),
        ("max_results bool", {"query": "a", "max_results": True}),
    ]
    for label, params in bad:
        try:
            tool.validate(params)
        except mod.InvalidParamsError as exc:
            assert "params" in str(exc), (label, exc)
        else:
            raise AssertionError("find_instances accepted invalid params: " + label)

    # execute rejects invalid params up front: nothing is sent to the plugin.
    rbx = FakeRBX(find_instances_response())
    registry = mod.default_registry()
    for params in ({"query": ""}, {"query": "a", "max_results": 0}, {"max_results": 5}):
        try:
            registry.execute(rbx, "find_instances", params, timeout=5.0)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("execute accepted invalid find_instances params: {0!r}"
                                 .format(params))
    assert rbx.requests == [], rbx.requests
    print("OK  invalid find_instances params rejected before sending "
          "(missing/empty query, bad max_results)")


def scenario_find_instances_failure():
    """A failed find_instances (plugin replies ok:false) must be reported and
    yield a non-zero exit code."""
    proc = Proc("--find-instances-once", "--query", "Baseplate", "--port", "0")
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
                "error": {"code": "execution_failed", "message": "boom"},
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 4, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("find_instances FAILED"), proc._output()
        assert proc.reader.contains("execution_failed"), proc._output()
        print("OK  find_instances failure response reported; non-zero exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_find_instances_roundtrip():
    """--find-instances-once must wait for the plugin, send a find_instances
    request with the query and max_results (default 20), accept a bounded match
    response, and exit 0."""
    proc = Proc("--find-instances-once", "--query", "Baseplate", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "find_instances", req
        assert req["payload"]["params"] == {"query": "Baseplate", "max_results": 20}, req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": find_instances_response(
                total=2, count=2, query="Baseplate", max_results=20,
                matches=[
                    {"name": "Baseplate", "className": "Part", "path": "Workspace/Baseplate"},
                    {"name": "Baseplate2", "className": "Part", "path": "Workspace/Folder/Baseplate2"},
                ],
            ),
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("find_instances OK: 2 match(es) for query 'Baseplate'"), \
            proc._output()
        print("OK  find_instances round-trip through the protocol; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_find_instances_roundtrip_truncated():
    """--find-instances-once --max-results N must pass N through and report a
    truncated result (in the log) when the plugin says more matches exist."""
    proc = Proc("--find-instances-once", "--query", "Part", "--max-results", "2", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "find_instances", req
        assert req["payload"]["params"] == {"query": "Part", "max_results": 2}, req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": find_instances_response(
                total=5, count=2, truncated=True, query="Part", max_results=2,
                matches=[
                    {"name": "Part1", "className": "Part", "path": "Workspace/Part1"},
                    {"name": "Part2", "className": "Part", "path": "Workspace/Part2"},
                ],
            ),
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("find_instances OK: 5 match(es) for query 'Part'"), \
            proc._output()
        assert proc.reader.contains("(truncated"), proc._output()
        print("OK  --max-results passed through and truncation reported (log + exit 0)")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


# --------------------------------------------------------------------------- #
# Phase 4C: inspect_instance scenarios
# --------------------------------------------------------------------------- #


def inspect_instance_response(name="Baseplate", className="Part",
                              path="Workspace/Baseplate", parent_path="Workspace",
                              properties=None):
    """A canned inspect_instance success result."""
    return {
        "ok": True,
        "result": {
            "name": name,
            "className": className,
            "path": path,
            "parent_path": parent_path,
            "properties": properties if properties is not None else {},
        },
    }


BASE_PART_PROPERTIES = {
    "Position": {"x": 0, "y": 5, "z": 0},
    "Size": {"x": 4, "y": 4, "z": 4},
    "Anchored": True,
    "CanCollide": True,
    "Transparency": 0.0,
}

SPAWN_LOCATION_PROPERTIES = {
    "Position": {"x": 0, "y": 7.5, "z": 0},
    "Size": {"x": 8, "y": 1.2, "z": 8},
    "Anchored": True,
    "CanCollide": True,
    "Transparency": 0.0,
    "Enabled": True,
    "Duration": 5.0,
    "Neutral": True,
    "TeamColor": {"name": "Bright red", "number": 21},
}


def scenario_inspect_instance_mock_response():
    """inspect_instance must run through the ToolRegistry, send the given path,
    and report the returned identity + serialized properties in the log."""
    mod = load_cli_module()
    rbx = FakeRBX(inspect_instance_response(properties=BASE_PART_PROPERTIES))
    registry = mod.default_registry()
    result = registry.execute(rbx, "inspect_instance", {"path": "Workspace.Baseplate"},
                              timeout=5.0)
    assert result is True, result
    assert rbx.requests == [("inspect_instance", {"path": "Workspace.Baseplate"})], rbx.requests
    expected = "inspect_instance OK: Workspace/Baseplate (Part): " + \
        json.dumps(BASE_PART_PROPERTIES)
    assert any(expected in line for line in rbx.logs), rbx.logs
    print("OK  inspect_instance executes through the ToolRegistry with a mock response")


def scenario_inspect_instance_spawn_location():
    """A SpawnLocation inspection carries the extended property set and the
    BrickColor (TeamColor) serialization through untouched."""
    mod = load_cli_module()
    rbx = FakeRBX(inspect_instance_response(
        className="SpawnLocation", path="Workspace/SpawnLocation",
        properties=SPAWN_LOCATION_PROPERTIES,
    ))
    registry = mod.default_registry()
    result = registry.execute(rbx, "inspect_instance", {"path": "Workspace.SpawnLocation"},
                              timeout=5.0)
    assert result is True, result
    expected = "inspect_instance OK: Workspace/SpawnLocation (SpawnLocation): " + \
        json.dumps(SPAWN_LOCATION_PROPERTIES)
    assert any(expected in line for line in rbx.logs), rbx.logs
    assert any('"TeamColor": {"name": "Bright red", "number": 21}' in line
               for line in rbx.logs), rbx.logs
    print("OK  SpawnLocation inspection: extended properties + TeamColor serialize")


def scenario_inspect_instance_property_serialization():
    """The wire contract for the supported value types (Vector3 / Color3 /
    EnumItem) renders through the CLI, and unlisted classes have no properties."""
    mod = load_cli_module()
    registry = mod.default_registry()
    props = {
        "Position": {"x": 1, "y": 2, "z": 3},          # Vector3
        "Color": {"r": 0.5, "g": 0.25, "b": 0.1},      # Color3
        "SurfaceType": {"name": "RobloxLocked", "value": 2},  # EnumItem
    }
    rbx = FakeRBX(inspect_instance_response(properties=props))
    assert registry.execute(rbx, "inspect_instance", {"path": "Workspace/Door"},
                            timeout=5.0) is True
    rendered = [line for line in rbx.logs if "inspect_instance OK" in line]
    assert rendered, rbx.logs
    assert '"Position": {"x": 1, "y": 2, "z": 3}' in rendered[0], rendered
    assert '"Color": {"r": 0.5, "g": 0.25, "b": 0.1}' in rendered[0], rendered
    assert '"SurfaceType": {"name": "RobloxLocked", "value": 2}' in rendered[0], rendered

    # An unlisted class returns identity/path only (no properties).
    rbx = FakeRBX(inspect_instance_response(
        name="Folder", className="Folder", path="Workspace/Folder",
    ))
    assert registry.execute(rbx, "inspect_instance", {"path": "Workspace.Folder"},
                            timeout=5.0) is True
    assert any("inspect_instance OK: Workspace/Folder (Folder): {}" in line
               for line in rbx.logs), rbx.logs
    print("OK  Vector3/Color3/EnumItem shapes render; unlisted class has no properties")


def scenario_inspect_instance_validation():
    """path must be a non-empty string or the call is rejected with no request
    sent (the plugin enforces the full path format and not-found lookups)."""
    mod = load_cli_module()
    tool = mod.default_registry().get("inspect_instance")
    assert tool is not None

    good = [
        {"path": "Workspace.Baseplate"},
        {"path": "Workspace/Shop/Door"},
        {"path": "Workspace.Parts.Folder.Crystal"},
    ]
    for params in good:
        tool.validate(params)  # must not raise

    bad = [
        ("missing path", {}),
        ("empty path", {"path": ""}),
        ("path not a string", {"path": 42}),
        ("path not a string (list)", {"path": ["Workspace"]}),
    ]
    for label, params in bad:
        try:
            tool.validate(params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("inspect_instance accepted invalid params: " + label)

    rbx = FakeRBX(inspect_instance_response())
    registry = mod.default_registry()
    for params in ({"path": ""}, {}):
        try:
            registry.execute(rbx, "inspect_instance", params, timeout=5.0)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("execute accepted invalid inspect_instance params: {0!r}"
                                 .format(params))
    assert rbx.requests == [], rbx.requests
    print("OK  invalid inspect_instance paths rejected before sending (missing/empty/non-string)")


def scenario_inspect_instance_not_found():
    """A plugin 'not_found' reply is surfaced as a failed inspect_instance log
    and a non-zero exit code."""
    proc = Proc("--inspect-instance-once", "--path", "Workspace.NoSuch", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "inspect_instance", req
        assert req["payload"]["params"] == {"path": "Workspace.NoSuch"}, req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": False,
                "error": {"code": "not_found",
                          "message": "instance not found at path: Workspace.NoSuch"},
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 4, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("inspect_instance FAILED"), proc._output()
        assert proc.reader.contains("not_found"), proc._output()
        print("OK  not-found inspect_instance reported (log + non-zero exit)")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_inspect_instance_roundtrip():
    """--inspect-instance-once must wait for the plugin, send an inspect_instance
    request with the given path, accept identity + properties, and exit 0."""
    proc = Proc("--inspect-instance-once", "--path", "Workspace.SpawnLocation", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "inspect_instance", req
        assert req["payload"]["params"] == {"path": "Workspace.SpawnLocation"}, req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": inspect_instance_response(
                className="SpawnLocation", path="Workspace/SpawnLocation",
                properties=SPAWN_LOCATION_PROPERTIES,
            ),
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("inspect_instance OK: Workspace/SpawnLocation "
                                    "(SpawnLocation)"), proc._output()
        assert proc.reader.contains('"Enabled": true'), proc._output()
        print("OK  inspect_instance round-trip through the protocol; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


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


def scenario_create_part_physics_round_trip():
    """Phase 5B real protocol round-trip: the create_part request sent over the
    WebSocket must carry the default ``anchored``/``can_collide`` booleans and
    the default Phase 5C ``material`` ("Plastic"), and the plugin echo of those
    properties is accepted end-to-end."""
    proc = Proc("--create-part-once", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_part", req
        params = req["payload"]["params"]
        assert params["name"] == "RBXForgeTestPart", params
        assert params["anchored"] is True, params
        assert params["can_collide"] is True, params
        assert params["material"] == "Plastic", params
        assert params["color"] == "red", params

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
                    "material": "Plastic",
                    "anchored": True,
                    "can_collide": True,
                },
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("create_part OK"), proc._output()
        print("OK  create_part physics + material round-trip over the protocol (defaults sent+echoed)")
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
                "error": {"code": "invalid_params", "message": "unsupported color: purple"},
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


def scenario_create_script_success():
    """--create-script-once must wait for the plugin, send a create_script
    request with the fixed test parameters, accept a success response, and
    exit 0."""
    proc = Proc("--create-script-once", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_script", req
        assert req["payload"]["params"]["name"] == "RBXForgeTestScript", req
        assert req["payload"]["params"]["type"] == "Script", req
        assert req["payload"]["params"]["parent_path"] == "ServerScriptService", req
        assert req["payload"]["params"]["source"] == 'print("Hello from RBXForge")\n', req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": True,
                "result": {
                    "name": "RBXForgeTestScript",
                    "type": "Script",
                    "parent_path": "ServerScriptService",
                    "path": "ServerScriptService/RBXForgeTestScript",
                    "source_length": 28,
                },
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("create_script OK"), proc._output()
        print("OK  create_script request sent; success response handled; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_create_script_failure():
    """A failed create_script (plugin replies ok:false) must be reported and
    yield a non-zero exit code."""
    proc = Proc("--create-script-once", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_script", req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": False,
                "error": {"code": "not_found", "message": "parent not found at path: ServerScriptService"},
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 4, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("create_script FAILED"), proc._output()
        assert proc.reader.contains("not_found"), proc._output()
        print("OK  create_script failure response reported; non-zero exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_interactive_create_script_registered():
    """The interactive REPL must recognize the 'create_script' command.

    Mirror of scenario_interactive_create_part_registered: the command must be
    dispatched (to the no-connection path) rather than rejected as unknown.
    """
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()
        assert proc.reader.wait_for("Type 'help'"), proc._output()

        proc.proc.stdin.write(b"create_script\n")
        proc.proc.stdin.flush()
        proc.reader.wait_for("cannot execute create_script")
        assert not proc.reader.contains("unknown command"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  interactive REPL recognizes create_script (not 'unknown command')")
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


def scenario_tool_registry_metadata():
    """The tool registry must expose create_part, create_script, modify_instance,
    find_instances, inspect_hierarchy, inspect_instance, asset_search,
    recommend_assets, insert_asset, build, and plan_build with metadata."""
    mod = load_cli_module()
    registry = mod.default_registry()
    tools = registry.list()
    assert [t.name for t in tools] == [
        "asset_search", "build", "create_part", "create_script",
        "find_instances", "insert_asset", "inspect_hierarchy",
        "inspect_instance", "modify_instance", "plan_build",
        "recommend_assets",
    ], tools

    tool = registry.get("create_part")
    assert tool is not None
    assert isinstance(tool.description, str) and tool.description
    assert isinstance(tool.input_schema, dict)
    assert tool.input_schema["type"] == "object"
    assert "name" in tool.input_schema["properties"]
    assert set(tool.input_schema["required"]) == {"name", "position", "size", "color"}

    script = registry.get("create_script")
    assert script is not None
    assert isinstance(script.description, str) and script.description
    assert script.input_schema["type"] == "object"
    assert set(script.input_schema["required"]) == {"name"}
    name_prop = script.input_schema["properties"]["name"]
    assert name_prop == {"type": "string", "min_length": 1}
    type_prop = script.input_schema["properties"]["type"]
    assert type_prop == {"type": "string", "enum": mod.CREATE_SCRIPT_TYPES, "default": "Script"}
    assert mod.CREATE_SCRIPT_TYPES == ["Script", "LocalScript", "ModuleScript"]
    parent_prop = script.input_schema["properties"]["parent_path"]
    assert parent_prop == {"type": "string", "min_length": 1}
    source_prop = script.input_schema["properties"]["source"]
    assert source_prop == {"type": "string", "default": ""}

    modifier = registry.get("modify_instance")
    assert modifier is not None
    assert isinstance(modifier.description, str) and modifier.description
    assert modifier.input_schema["type"] == "object"
    assert set(modifier.input_schema["required"]) == {"path", "properties"}
    assert modifier.input_schema["properties"]["path"] == {"type": "string", "min_length": 1}
    inner = modifier.input_schema["properties"]["properties"]
    assert inner["type"] == "object"
    assert inner["additionalProperties"] is False  # explicit allowlist only
    assert set(inner["properties"].keys()) == {
        "position", "size", "anchored", "can_collide", "transparency", "color",
        "material", "enabled", "duration", "neutral", "team_color",
    }, inner
    assert inner["properties"]["transparency"] == {
        "type": "number", "minimum": 0, "maximum": 1,
    }
    assert inner["properties"]["color"]["enum"] == mod.CREATE_PART_COLORS
    assert inner["properties"]["material"]["enum"] == mod.CREATE_PART_MATERIALS
    assert inner["properties"]["duration"] == {"type": "number", "minimum": 0}
    assert inner["properties"]["team_color"]["enum"] == mod.MODIFY_TEAM_COLORS
    assert mod.MODIFY_TEAM_COLORS == [
        "Really red", "Bright blue", "Bright green", "Bright yellow", "White",
        "Black", "Medium stone grey",
    ]

    hierarchy = registry.get("inspect_hierarchy")
    assert hierarchy is not None
    assert isinstance(hierarchy.description, str) and hierarchy.description
    assert hierarchy.input_schema["type"] == "object"
    depth_prop = hierarchy.input_schema["properties"]["depth"]
    assert depth_prop["type"] == "number"
    assert depth_prop["integer"] is True
    assert depth_prop["minimum"] == 1
    assert depth_prop["maximum"] == mod.MAX_HIERARCHY_DEPTH

    finder = registry.get("find_instances")
    assert finder is not None
    assert isinstance(finder.description, str) and finder.description
    assert finder.input_schema["type"] == "object"
    query_prop = finder.input_schema["properties"]["query"]
    assert query_prop["type"] == "string"
    assert query_prop["min_length"] == 1
    assert set(finder.input_schema["required"]) == {"query"}
    max_prop = finder.input_schema["properties"]["max_results"]
    assert max_prop["type"] == "number"
    assert max_prop["integer"] is True
    assert max_prop["minimum"] == 1
    assert max_prop["maximum"] == mod.MAX_FIND_RESULTS

    inspector = registry.get("inspect_instance")
    assert inspector is not None
    assert isinstance(inspector.description, str) and inspector.description
    assert inspector.input_schema["type"] == "object"
    path_prop = inspector.input_schema["properties"]["path"]
    assert path_prop["type"] == "string"
    assert path_prop["min_length"] == 1
    assert set(inspector.input_schema["required"]) == {"path"}

    inserter = registry.get("insert_asset")
    assert inserter is not None
    assert isinstance(inserter.description, str) and inserter.description
    assert inserter.input_schema["type"] == "object"
    assert set(inserter.input_schema["required"]) == {"asset_id"}
    asset_id_prop = inserter.input_schema["properties"]["asset_id"]
    assert asset_id_prop == {
        "type": "string", "min_length": 1, "max_length": 16, "pattern": "^[0-9]+$",
    }, asset_id_prop
    assert inserter.input_schema["properties"]["parent_path"] == {"type": "string", "min_length": 1}
    assert inserter.input_schema["properties"]["position"] == {"type": "vec3"}
    assert inserter.input_schema["properties"]["reference_path"] == {"type": "string", "min_length": 1}
    assert mod.INSERTABLE_ASSET_TYPES == frozenset({"Model", "MeshPart", "Decal", "Audio"})
    assert mod.MAX_KNOWN_ASSETS == 200

    builder = registry.get("build")
    assert builder is not None
    assert isinstance(builder.description, str) and builder.description
    assert builder.input_schema["type"] == "object"
    assert set(builder.input_schema["required"]) == {"description"}
    assert builder.input_schema["properties"]["description"] == {
        "type": "string", "min_length": 1, "max_length": 500,
    }
    assert builder.input_schema["properties"]["reference_path"] == {
        "type": "string", "min_length": 1,
    }

    planner = registry.get("plan_build")
    assert planner is not None
    assert isinstance(planner.description, str) and planner.description
    assert planner.input_schema["type"] == "object"
    assert set(planner.input_schema["required"]) == {"description", "steps"}
    assert planner.input_schema["properties"]["description"] == {
        "type": "string", "min_length": 1, "max_length": 500,
    }
    assert planner.input_schema["properties"]["reference_path"] == {
        "type": "string", "min_length": 1,
    }
    steps_schema = planner.input_schema["properties"]["steps"]
    assert steps_schema["type"] == "array"
    assert steps_schema["minItems"] == 1
    assert steps_schema["maxItems"] == mod.PLAN_MAX_STEPS
    item = steps_schema["items"]
    assert set(item["required"]) == {"tool", "arguments"}
    assert item["properties"]["tool"] == {"type": "string", "min_length": 1}
    assert item["properties"]["arguments"] == {"type": "object"}
    print("OK  registry registers create_part, create_script, modify_instance, "
          "find_instances, inspect_hierarchy, inspect_instance, insert_asset, "
          "build, and plan_build with metadata")


def scenario_tool_validation():
    """create_part arguments must be validated against its schema before use."""
    mod = load_cli_module()
    valid = dict(mod.CREATE_PART_DEFAULT_PARAMS)
    tool = mod.default_registry().get("create_part")

    # Valid parameters pass (the fixed Phase 2A defaults, plus a variation).
    tool.validate(valid)
    tool.validate({
        "name": "MyPart",
        "position": {"x": 1, "y": 2, "z": 3},
        "size": {"x": -2, "y": 4, "z": 6},
        "color": "red",
    })

    invalid_cases = [
        ("missing required property", {}),
        ("empty name", {k: v if k != "name" else "" for k, v in valid.items()}),
        ("missing z of position", {
            k: v if k != "position" else {"x": 0, "y": 1} for k, v in valid.items()
        }),
        ("position not an object", {k: v if k != "position" else "5,5,5" for k, v in valid.items()}),
        ("unsupported color", {k: v if k != "color" else "purple" for k, v in valid.items()}),
        ("name not a string", {k: v if k != "name" else 42 for k, v in valid.items()}),
        ("params not an object", ["nope"]),
    ]
    for label, bad_params in invalid_cases:
        try:
            tool.validate(bad_params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_part accepted invalid params: " + label)
    print("OK  create_part schema rejects invalid arguments (missing/wrong type/value)")


def scenario_create_part_all_colors():
    """create_part must accept exactly the supported colors (Phase 5A) and
    reject every unsupported color, keeping the enum strict and red as-is."""
    mod = load_cli_module()
    valid = dict(mod.CREATE_PART_DEFAULT_PARAMS)
    tool = mod.default_registry().get("create_part")

    expected = ["red", "blue", "green", "yellow", "white", "black", "gray"]
    assert mod.CREATE_PART_COLORS == expected, mod.CREATE_PART_COLORS
    assert tool.input_schema["properties"]["color"]["enum"] == expected, \
        tool.input_schema["properties"]["color"]

    # Every supported color passes the CLI schema (red unchanged).
    for color in expected:
        validated = tool.validate({k: v if k != "color" else color for k, v in valid.items()})
        assert validated["color"] == color, validated

    # Unsupported colors (and non-string / empty / case-mismatched values) are
    # rejected before anything is sent.
    for color in ("purple", "orange", "cyan", "pink", "brown", "magenta", "navy",
                  "Red", "RED", "", 42, None):
        try:
            tool.validate({k: v if k != "color" else color for k, v in valid.items()})
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_part accepted unsupported color: {0!r}".format(color))
    print("OK  create_part accepts all 7 colors and rejects unsupported colors")


def scenario_create_part_physics_validation():
    """Phase 5B: ``anchored`` / ``can_collide`` are optional booleans defaulting
    to true. The CLI exposes the defaults, keeps existing calls unchanged,
    accepts every explicit true/false combination, still accepts all 7 colors,
    and rejects wrong types before anything is sent."""
    mod = load_cli_module()
    tool = mod.default_registry().get("create_part")
    base = {
        "name": "PhysPart",
        "position": {"x": 0, "y": 1, "z": 0},
        "size": {"x": 2, "y": 2, "z": 2},
        "color": "red",
    }

    # Schema: optional booleans with CLI defaults; not required.
    assert tool.input_schema["properties"]["anchored"] == {"type": "boolean", "default": True}
    assert tool.input_schema["properties"]["can_collide"] == {"type": "boolean", "default": True}
    assert tool.input_schema["required"] == ["name", "position", "size", "color"]

    # CLI defaults keep a freshly created part anchored and collidable by default.
    asserted_default = mod.CREATE_PART_DEFAULT_PARAMS
    assert asserted_default["anchored"] is True, asserted_default
    assert asserted_default["can_collide"] is True, asserted_default

    # Existing calls that omit the physics flags validate and send unchanged
    # (the plugin applies the defaults on its side).
    validated = tool.validate(dict(base))
    assert "anchored" not in validated and "can_collide" not in validated, validated

    # Every explicit combination validates and is executed through the registry
    # with the exact parameters the caller supplied.
    combos = [
        {"anchored": False},
        {"can_collide": False},
        {"anchored": False, "can_collide": False},
        {"anchored": True, "can_collide": True},
    ]
    for extra in combos:
        params = dict(base)
        params.update(extra)
        rbx = FakeRBX({"ok": True, "result": {"name": "PhysPart"}})
        ok = mod.default_registry().execute(rbx, "create_part", params)
        assert ok is True, ok
        assert rbx.requests == [("create_part", params)], rbx.requests

    # All 7 colors still pass alongside the physics flags.
    for color in mod.CREATE_PART_COLORS:
        params = {"name": "ColorPart", "position": {"x": 0, "y": 0, "z": 0},
                  "size": {"x": 1, "y": 1, "z": 1}, "color": color,
                  "anchored": False, "can_collide": False}
        tool.validate(params)

    # Wrong types are rejected before sending (nothing hits the wire).
    rbx = FakeRBX({"ok": True})
    for key in ("anchored", "can_collide"):
        for bad in ("yes", 1, 0, 2.5, None, [], {}, "false", "true"):
            params = dict(base)
            params[key] = bad
            try:
                mod.default_registry().execute(rbx, "create_part", params)
            except mod.InvalidParamsError:
                pass
            else:
                raise AssertionError(
                    "create_part accepted {0}={1!r}".format(key, bad))
    assert rbx.requests == [], rbx.requests
    print("OK  create_part physics: default true, explicit combos, wrong types rejected")


def scenario_create_part_material_validation():
    """Phase 5C: ``material`` is optional, defaults to Plastic, is validated as
    a strict string enum, and invalid values are rejected before any send."""
    mod = load_cli_module()
    tool = mod.default_registry().get("create_part")

    expected = [
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

    base = {
        "name": "MatPart",
        "position": {"x": 0, "y": 1, "z": 0},
        "size": {"x": 2, "y": 2, "z": 2},
        "color": "red",
    }

    # Single source of truth is exactly this enum, mirrored in the schema.
    assert mod.CREATE_PART_MATERIALS == expected, mod.CREATE_PART_MATERIALS
    material_prop = tool.input_schema["properties"]["material"]
    assert material_prop["type"] == "string", material_prop
    assert material_prop["enum"] == expected, material_prop
    assert material_prop["default"] == "Plastic", material_prop
    assert "material" not in tool.input_schema["required"], tool.input_schema["required"]
    assert mod.CREATE_PART_DEFAULT_PARAMS["material"] == "Plastic", mod.CREATE_PART_DEFAULT_PARAMS

    # Existing calls that omit material remain valid.
    validated = tool.validate(dict(base))
    assert "material" not in validated, validated

    # Every supported material validates and can execute through the registry.
    for material in expected:
        params = dict(base)
        params["material"] = material
        params["anchored"] = False
        params["can_collide"] = False
        validated = tool.validate(params)
        assert validated["material"] == material, validated
        rbx = FakeRBX({"ok": True, "result": {"name": "MatPart", "material": material}})
        ok = mod.default_registry().execute(rbx, "create_part", params)
        assert ok is True, ok
        assert rbx.requests == [("create_part", params)], rbx.requests

    # Unsupported/case-mismatched spellings are rejected.
    for material in ("WoodPlank", "plastic", "Plastic ", "Granitee", "NEON"):
        try:
            tool.validate(dict(base, material=material))
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_part accepted unsupported material: {0!r}".format(material))

    # Non-string materials are rejected.
    for material in (42, None, {}, True):
        try:
            tool.validate(dict(base, material=material))
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_part accepted non-string material: {0!r}".format(material))

    # Invalid material is rejected before anything is sent.
    rbx = FakeRBX({"ok": True, "result": {"name": "NeverSent"}})
    for material in ("plastic", 42, None, {}, True):
        params = dict(base)
        params["material"] = material
        try:
            mod.default_registry().execute(rbx, "create_part", params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_part sent invalid material: {0!r}".format(material))
    assert rbx.requests == [], rbx.requests

    # Existing colors and physics flags still validate alongside material.
    for color in mod.CREATE_PART_COLORS:
        tool.validate({
            "name": "CompatibilityPart",
            "position": {"x": 0, "y": 0, "z": 0},
            "size": {"x": 1, "y": 1, "z": 1},
            "color": color,
            "material": "Neon",
            "anchored": False,
            "can_collide": True,
        })
    print("OK  create_part material: enum/default/optional + compatibility validation")


def scenario_create_script_validation():
    """create_script must validate name/type/parent_path/source against its
    schema, apply the CLI defaults, and never send invalid arguments."""
    mod = load_cli_module()
    tool = mod.default_registry().get("create_script")
    valid = dict(mod.CREATE_SCRIPT_DEFAULT_PARAMS)

    # Schema: name required; type/parent_path/source optional with defaults.
    assert tool.input_schema["required"] == ["name"]
    assert tool.input_schema["properties"]["type"] == {
        "type": "string", "enum": ["Script", "LocalScript", "ModuleScript"], "default": "Script",
    }
    assert tool.input_schema["properties"]["source"] == {"type": "string", "default": ""}
    assert mod.CREATE_SCRIPT_DEFAULT_PARAMS["type"] == "Script"
    assert mod.CREATE_SCRIPT_DEFAULT_PARAMS["parent_path"] == "ServerScriptService"

    # The happy path (fixed test params, plus a variation) validates.
    tool.validate(valid)
    tool.validate({
        "name": "MyModule",
        "type": "ModuleScript",
        "parent_path": "ReplicatedStorage.Gameplay",
        "source": 'local x = 1',
    })

    invalid_cases = [
        ("missing required property", {}),
        ("empty name", {k: v if k != "name" else "" for k, v in valid.items()}),
        ("name not a string", {k: v if k != "name" else 42 for k, v in valid.items()}),
        ("unsupported type", {k: v if k != "type" else "RemoteScript" for k, v in valid.items()}),
        ("type case mismatch", {k: v if k != "type" else "script" for k, v in valid.items()}),
        ("type not a string", {k: v if k != "type" else 7 for k, v in valid.items()}),
        ("empty parent_path", {k: v if k != "parent_path" else "" for k, v in valid.items()}),
        ("parent_path not a string", {k: v if k != "parent_path" else 1 for k, v in valid.items()}),
        ("source not a string", {k: v if k != "source" else ["print()"] for k, v in valid.items()}),
        ("params not an object", "nope"),
    ]
    for label, bad_params in invalid_cases:
        try:
            tool.validate(bad_params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_script accepted invalid params: " + label)

    # Invalid arguments are rejected before anything is sent.
    rbx = FakeRBX({"ok": True, "result": {"name": "NeverSent"}})
    for bad_params in ({"name": ""}, {"name": "X", "type": "CoreScript"},
                       {"name": "X", "parent_path": ""}):
        try:
            mod.default_registry().execute(rbx, "create_script", bad_params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("create_script sent invalid params: {0!r}".format(bad_params))
    assert rbx.requests == [], rbx.requests

    # A valid default call sends exactly the fixed params.
    ok = mod.default_registry().execute(rbx, "create_script", valid)
    assert ok is True, ok
    assert rbx.requests == [("create_script", valid)], rbx.requests
    print("OK  create_script schema: required name + enum/defaults + validation")


def scenario_modify_instance_validation():
    """modify_instance must validate path + the property allowlist strictly:
    valid BasePart and SpawnLocation modifications pass, unknown properties,
    wrong types, out-of-range values, and invalid colors/materials/team colors
    are rejected, and nothing is sent when CLI validation fails."""
    mod = load_cli_module()
    registry = mod.default_registry()
    tool = registry.get("modify_instance")
    assert tool.input_schema["required"] == ["path", "properties"]

    # -- valid requests (nothing sent yet, just validation) ------------------ #
    valid_basepart = {
        "path": "Workspace.MyPart",
        "properties": {
            "position": {"x": 0, "y": 5, "z": 0},
            "size": {"x": 2, "y": 2, "z": 2},
            "anchored": False,
            "can_collide": True,
            "transparency": 0.5,
            "color": "red",
            "material": "Neon",
        },
    }
    tool.validate(valid_basepart)

    # Multiple properties in one request (SpawnLocation extras).
    valid_spawn = {
        "path": "Workspace.SpawnLocation",
        "properties": {
            "enabled": False,
            "duration": 3,
            "neutral": True,
            "team_color": "Bright blue",
            "transparency": 0,
            "anchored": True,
        },
    }
    tool.validate(valid_spawn)

    # Boundary values are valid: transparency 0 and 1, duration 0.
    tool.validate({
        "path": "Workspace.P",
        "properties": {"transparency": 1, "duration": 0},
    })

    # -- invalid requests: rejected by the CLI, nothing sent ---------------- #
    invalid_cases = [
        ("missing path", {"properties": {"anchored": True}}),
        ("empty path", {"path": "", "properties": {"anchored": True}}),
        ("missing properties", {"path": "Workspace.P"}),
        ("properties not an object", {"path": "Workspace.P", "properties": "nope"}),
        ("properties empty object", {"path": "Workspace.P", "properties": {}}),
        ("unknown property", {"path": "Workspace.P", "properties": {"unknown_key": 1}}),
        ("unknown property model name", {
            "path": "Workspace.P", "properties": {"Position": 1},
        }),
        ("unknown nested in otherwise-valid", {
            "path": "Workspace.P", "properties": {"anchored": True, "bogus": "x"},
        }),
        ("wrong type boolean", {
            "path": "Workspace.P", "properties": {"anchored": "yes"},
        }),
        ("wrong type vec3", {
            "path": "Workspace.P", "properties": {"position": [0, 5, 0]},
        }),
        ("vec3 missing axis", {
            "path": "Workspace.P", "properties": {"size": {"x": 1, "y": 2}},
        }),
        ("invalid transparency high", {
            "path": "Workspace.P", "properties": {"transparency": 1.5},
        }),
        ("invalid transparency low", {
            "path": "Workspace.P", "properties": {"transparency": -0.1},
        }),
        ("transparency not a number", {
            "path": "Workspace.P", "properties": {"transparency": "half"},
        }),
        ("invalid duration negative", {
            "path": "Workspace.P", "properties": {"duration": -1},
        }),
        ("duration not a number", {
            "path": "Workspace.P", "properties": {"duration": None},
        }),
        ("invalid color", {
            "path": "Workspace.P", "properties": {"color": "purple"},
        }),
        ("color case mismatch", {
            "path": "Workspace.P", "properties": {"color": "Red"},
        }),
        ("invalid material", {
            "path": "Workspace.P", "properties": {"material": "plastic"},
        }),
        ("invalid team color", {
            "path": "Workspace.P", "properties": {"team_color": "Hot pink"},
        }),
        ("team color case mismatch", {
            "path": "Workspace.P", "properties": {"team_color": "bright blue"},
        }),
    ]
    rbx = FakeRBX({"ok": True, "result": {"path": "Workspace.P", "className": "Part", "changed": {}}})
    for label, bad_params in invalid_cases:
        try:
            registry.execute(rbx, "modify_instance", bad_params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("modify_instance accepted invalid params: " + label)
    assert rbx.requests == [], rbx.requests

    # -- a valid call sends exactly what was validated ---------------------- #
    ok = registry.execute(rbx, "modify_instance", valid_basepart)
    assert ok is True, ok
    assert rbx.requests == [("modify_instance", valid_basepart)], rbx.requests
    print("OK  modify_instance: allowlist + types + ranges validated; "
          "unknown/out-of-range rejected before send")
    print("OK  modify_instance valid BasePart + SpawnLocation requests pass")
    print("OK  modify_instance valid multiple-property request passes")


def scenario_modify_instance_success_roundtrip():
    """--modify-instance-once must wait for the plugin, send a modify_instance
    request with the given --path/--properties, accept a success response, and
    exit 0."""
    proc = Proc("--modify-instance-once", "--path", "Workspace.SpawnLocation",
                "--properties", '{"neutral": true, "enabled": true}', "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "modify_instance", req
        assert req["payload"]["params"]["path"] == "Workspace.SpawnLocation", req
        assert req["payload"]["params"]["properties"] == {"neutral": True, "enabled": True}, req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": True,
                "result": {
                    "path": "Workspace/SpawnLocation",
                    "className": "SpawnLocation",
                    "changed": {"neutral": True, "enabled": True},
                },
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("modify_instance OK"), proc._output()
        print("OK  modify_instance request sent; success response handled; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_modify_instance_failure():
    """A failed modify_instance (plugin replies ok:false, e.g. plugin-side
    validation or not_found) must be reported and yield a non-zero exit code."""
    proc = Proc("--modify-instance-once", "--path", "Workspace.Missing",
                "--properties", '{"anchored": true}', "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "modify_instance", req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": False,
                "error": {"code": "not_found", "message": "instance not found at path: Workspace.Missing"},
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 4, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("modify_instance FAILED"), proc._output()
        assert proc.reader.contains("not_found"), proc._output()
        print("OK  modify_instance failure response (plugin-side) reported; non-zero exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_insert_asset_validation():
    """insert_asset must validate its schema: an asset_id must be a digits-only
    string (bounded length), optional params have the right shapes, and a
    position + reference_path pair is rejected before anything is sent."""
    mod = load_cli_module()
    tool = mod.default_registry().get("insert_asset")
    rbx = FakeRBX({"ok": True, "result": {"asset_id": "135522", "name": "Cafe Shop"}})
    rbx.remember_assets([{"asset_id": "135522", "asset_type": "Model", "name": "Cafe Shop"}])

    # Valid: id alone, and each optional param alone.
    tool.validate({"asset_id": "135522"})
    tool.validate({"asset_id": "135522", "parent_path": "Workspace"})
    tool.validate({"asset_id": "135522", "position": {"x": 0, "y": 5, "z": 0}})
    tool.validate({"asset_id": "135522", "reference_path": "Workspace.SpawnLocation"})

    # Invalid ids: missing, empty, non-digits, absurdly long.
    invalid_cases = [
        ("missing asset_id", {}),
        ("empty asset_id", {"asset_id": ""}),
        ("non-digit asset_id", {"asset_id": "1355a2"}),
        ("decimal asset_id", {"asset_id": "1355.2"}),
        ("spaced asset_id", {"asset_id": "135 522"}),
        ("overlong asset_id", {"asset_id": "1" * 17}),
        ("asset_id not a string", {"asset_id": 135522}),
        ("position not vec3", {"asset_id": "1", "position": "5,5,5"}),
        ("parent_path empty", {"asset_id": "1", "parent_path": ""}),
    ]
    for label, bad_params in invalid_cases:
        try:
            tool.validate(bad_params)
        except mod.InvalidParamsError:
            pass
        else:
            raise AssertionError("insert_asset accepted invalid params: " + label)

    # Nothing reached the wire for the rejected cases.
    assert rbx.requests == [], rbx.requests
    print("OK  insert_asset schema rejects malformed ids and wrong param shapes")


def scenario_insert_asset_known_ids_enforced():
    """insert_asset must only accept an asset id that a prior search/ranking
    recorded (Phase 7C known-id enforcement) and only insertable asset types;
    anything else is rejected locally and never sent."""
    mod = load_cli_module()
    registry = mod.default_registry()

    # Invented id: never sent, clear rejection log.
    rbx = FakeRBX({"ok": True})
    ok = registry.execute(rbx, "insert_asset", {"asset_id": "999999"})
    assert ok is False, ok
    assert rbx.requests == [], rbx.requests
    assert any("not returned by a prior asset_search" in line for line in rbx.logs), rbx.logs

    # Recorded id of a non-insertable type: rejected, never sent.
    rbx = FakeRBX({"ok": True}, known_assets={"42": {"asset_type": "Plugin"}})
    ok = registry.execute(rbx, "insert_asset", {"asset_id": "42"})
    assert ok is False, ok
    assert rbx.requests == [], rbx.requests
    assert any("cannot be inserted" in line for line in rbx.logs), rbx.logs

    # recorded id of a missing asset_type is allowed through (metadata may be
    # absent); the plugin re-validates at load time.
    rbx = FakeRBX({"ok": True}, known_assets={"7": {"name": "Mystery"}})
    ok = registry.execute(rbx, "insert_asset", {"asset_id": "7"})
    assert ok is True, ok
    assert rbx.requests == [("insert_asset", {"asset_id": "7"})], rbx.requests

    # Recorded id of an insertable type succeeds and is sent exactly as given.
    rbx = FakeRBX({"ok": True}, known_assets={"135522": {"asset_type": "Model"}})
    ok = registry.execute(rbx, "insert_asset", {"asset_id": "135522"})
    assert ok is True, ok
    assert rbx.requests == [("insert_asset", {"asset_id": "135522"})], rbx.requests

    # position + reference_path are mutually exclusive: rejected before send.
    rbx = FakeRBX({"ok": True}, known_assets={"1": {"asset_type": "Model"}})
    ok = registry.execute(rbx, "insert_asset", {
        "asset_id": "1", "position": {"x": 0, "y": 1, "z": 0},
        "reference_path": "Workspace.SpawnLocation",
    })
    assert ok is False, ok
    assert rbx.requests == [], rbx.requests
    assert any("not both" in line for line in rbx.logs), rbx.logs

    # remember_assets records the snapshot needed for later insertion.
    rbx = FakeRBX({"ok": True})
    assert rbx.remember_assets([{"asset_id": "A", "name": "Shop", "asset_type": "Model"}]) == 1
    assert rbx.asset_known("A") is True
    assert rbx.known_asset("A")["asset_type"] == "Model"
    print("OK  insert_asset enforces known ids, insertable types, and one placement mode")


def scenario_insert_asset_bounded_registry():
    """The known-assets registry stays bounded (MAX_KNOWN_ASSETS): after the
    limit is exceeded the oldest records are dropped, newest survive."""
    mod = load_cli_module()
    rbx = mod.RBXForge()
    try:
        records = [{"asset_id": str(i), "name": "asset{0}".format(i)} for i in range(mod.MAX_KNOWN_ASSETS + 25)]
        rbx.remember_assets(records)
        # Oldest are evicted once the table exceeds the bound.
        assert rbx.asset_known("0") is False
        assert rbx.asset_known("1") is False
        # Newest retained.
        assert rbx.asset_known(str(mod.MAX_KNOWN_ASSETS + 24)) is True
        assert len(rbx._known_assets) == mod.MAX_KNOWN_ASSETS

        # Non-dict / id-less entries are skipped gracefully.
        rbx.remember_assets([None, {}, {"name": "no id"}, {"asset_id": "  ", "name": "blank"}])
        assert len(rbx._known_assets) == mod.MAX_KNOWN_ASSETS
    finally:
        rbx.stop()
    print("OK  known-assets registry is bounded and tolerates junk entries")


def scenario_insert_asset_success():
    """--insert-asset-once must wait for the plugin, send an insert_asset request
    carrying only the seeded asset_id (no invented position), accept a success
    response including the plugin's final name/path, and exit 0."""
    proc = Proc("--insert-asset-once", "--asset-id", "135522", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "insert_asset", req
        assert req["payload"]["params"] == {"asset_id": "135522"}, req["payload"]["params"]
        assert "position" not in req["payload"]["params"], req["payload"]["params"]
        assert "reference_path" not in req["payload"]["params"], req["payload"]["params"]

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": True,
                "result": {
                    "asset_id": "135522",
                    "name": "Cafe Shop2",
                    "class": "Model",
                    "parent_path": "Workspace",
                    "path": "Workspace/Cafe Shop2",
                    "positioned": True,
                    "placement": "default",
                    "position": {"x": 0, "y": 5, "z": 0},
                },
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("insert_asset OK"), proc._output()
        assert proc.reader.contains("Cafe Shop2"), proc._output()
        assert proc.reader.contains("Workspace/Cafe Shop2"), proc._output()
        print("OK  insert_asset request sent; unique-name/placement result handled; clean exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_insert_asset_failure():
    """A failed insert_asset (plugin replies ok:false, e.g. not_found) must be
    reported and yield a non-zero exit code."""
    proc = Proc("--insert-asset-once", "--asset-id", "135522", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "insert_asset", req

        send_json(sock, {
            "type": "response",
            "id": req["id"],
            "version": PROTOCOL_VERSION,
            "timestamp": 0.0,
            "payload": {
                "ok": False,
                "error": {"code": "not_found", "message": "asset not found or not insertable: 135522"},
            },
        })
        sock.close()
        rc = proc.wait()
        assert rc == 4, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("insert_asset FAILED"), proc._output()
        assert proc.reader.contains("not_found"), proc._output()
        print("OK  insert_asset failure response (plugin-side) reported; non-zero exit")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_insert_asset_placement_modes():
    """--insert-asset-once placement: an explicit --position JSON or a
    --reference-path is carried through untouched, and neither triggers an
    invented-parent default."""
    proc = Proc("--insert-asset-once", "--asset-id", "135522",
                "--reference-path", "Workspace.SpawnLocation", "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()
        req = recv_json(sock)
        assert req["type"] == "request", req
        params = req["payload"]["params"]
        assert params["asset_id"] == "135522", params
        assert params["reference_path"] == "Workspace.SpawnLocation", params
        assert "position" not in params, params
        send_json(sock, {
            "type": "response", "id": req["id"], "version": PROTOCOL_VERSION,
            "timestamp": 0.0, "payload": {"ok": True, "result": {
                "asset_id": "135522", "name": "Cafe Shop", "class": "Model",
                "parent_path": "Workspace", "path": "Workspace/Cafe Shop",
                "positioned": True, "placement": "reference",
                "position": {"x": 5, "y": 0, "z": 0},
            }},
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("insert_asset OK"), proc._output()
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()

    proc = Proc("--insert-asset-once", "--asset-id", "135522",
                "--position", '{"x": 1, "y": 6, "z": -2}', "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()
        req = recv_json(sock)
        assert req["type"] == "request", req
        params = req["payload"]["params"]
        assert params["asset_id"] == "135522", params
        assert params["position"] == {"x": 1, "y": 6, "z": -2}, params
        assert "reference_path" not in params, params
        send_json(sock, {
            "type": "response", "id": req["id"], "version": PROTOCOL_VERSION,
            "timestamp": 0.0, "payload": {"ok": True, "result": {
                "asset_id": "135522", "name": "Cafe Shop", "class": "Model",
                "parent_path": "Workspace", "path": "Workspace/Cafe Shop",
                "positioned": True, "placement": "explicit",
                "position": {"x": 1, "y": 6, "z": -2},
            }},
        })
        sock.close()
        rc = proc.wait()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("insert_asset OK"), proc._output()
        print("OK  insert_asset carries --position and --reference-path through untouched")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_insert_asset_timeout():
    """insert_asset must respect the request timeout and report failure when the
    plugin gives no response (Phase 7D: delayed / missing response is not
    treated as success)."""
    mod = load_cli_module()
    rbx = FakeRBX(None)  # send_request returns None, simulating a timeout
    rbx.remember_assets([{"asset_id": "135522", "asset_type": "Model"}])
    ok = mod.default_registry().execute(
        rbx, "insert_asset", {"asset_id": "135522"}, timeout=0.01
    )
    assert ok is False, ok
    assert rbx.requests == [("insert_asset", {"asset_id": "135522"})], rbx.requests
    assert any("no response" in line for line in rbx.logs), rbx.logs
    print("OK  insert_asset timeout / no plugin response is reported as failure")


def scenario_insert_asset_cli_rejects_usage():
    """--insert-asset-once usage errors exit 2 before any insertion: a missing
    --asset-id, a position+reference_path conflict, and unparsable JSON."""
    proc = Proc("--insert-asset-once", "--asset-id", "1",
                "--position", '{"x": 0, "y": 0, "z": 0}',
                "--reference-path", "Workspace.SpawnLocation", "--port", "0")
    try:
        rc = proc.wait()
        assert rc == 2, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("may not both be given"), proc._output()
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()

    proc = Proc("--insert-asset-once", "--asset-id", "1",
                "--position", "not-json", "--port", "0")
    try:
        rc = proc.wait()
        assert rc == 2, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("must be valid JSON"), proc._output()
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()

    proc = Proc("--insert-asset-once", "--port", "0")
    try:
        rc = proc.wait()
        assert rc == 2, "exit code {}; output:\n{}".format(rc, proc._output())
        assert proc.reader.contains("--insert-asset-once requires --asset-id"), proc._output()
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()
    print("OK  --insert-asset-once usage errors exit 2 without inserting")


def scenario_interactive_insert_asset_registered():
    """The interactive REPL must recognize the 'insert_asset' command.

    Mirror of scenario_interactive_create_part_registered: the command must be
    dispatched (to the no-connection path) rather than rejected as unknown.
    """
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()
        assert proc.reader.wait_for("Type 'help'"), proc._output()

        proc.proc.stdin.write(b"insert_asset 135522 Workspace\n")
        proc.proc.stdin.flush()
        proc.reader.wait_for("cannot execute insert_asset")
        assert not proc.reader.contains("unknown command"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  interactive REPL recognizes insert_asset (not 'unknown command')")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_interactive_modify_instance_registered():
    """The interactive REPL must recognize the 'modify_instance' command.

    Mirror of scenario_interactive_create_part_registered: the command must be
    dispatched (to the no-connection path) rather than rejected as unknown.
    """
    proc = Proc("--port", "0")
    try:
        host, port = proc.listening_addr()
        assert proc.reader.wait_for("Type 'help'"), proc._output()

        proc.proc.stdin.write(b'modify_instance Workspace.SpawnLocation {"neutral": false}\n')
        proc.proc.stdin.flush()
        proc.reader.wait_for("cannot execute modify_instance")
        assert not proc.reader.contains("unknown command"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  interactive REPL recognizes modify_instance (not 'unknown command')")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_tool_invalid_rejected_before_send():
    """execute_tool must reject invalid params before attempting to send."""
    mod = load_cli_module()
    rbx = mod.RBXForge()

    # Invalid params: rejected locally, no request is attempted.
    result = rbx.execute_tool("create_part", {"name": ""}, timeout=1.0)
    assert result is False, result

    # Unknown tool: rejected locally too.
    result = rbx.execute_tool("does_not_exist", {}, timeout=1.0)
    assert result is False, result

    # Valid params but no connection: reaches the send stage (no plugin), proving
    # validation happened first and was not the reason for failure.
    result = rbx.execute_tool("create_part", dict(mod.CREATE_PART_DEFAULT_PARAMS), timeout=1.0)
    assert result is False, result

    # The registry itself is the same object the CLI would use.
    assert rbx.create_part(timeout=1.0) is False
    print("OK  execute_tool validates before sending; unknown tools rejected")
    rbx.stop()


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
    scenario_tool_registry_metadata()
    scenario_tool_validation()
    scenario_create_part_all_colors()
    scenario_create_part_physics_validation()
    scenario_create_part_material_validation()
    scenario_create_script_validation()
    scenario_modify_instance_validation()
    scenario_tool_invalid_rejected_before_send()
    scenario_inspect_hierarchy_mock_response()
    scenario_inspect_hierarchy_depth_semantics()
    scenario_inspect_hierarchy_empty_hierarchy()
    scenario_inspect_hierarchy_invalid_depth()
    scenario_find_instances_mock_response()
    scenario_find_instances_max_results_and_truncation()
    scenario_find_instances_validation()
    scenario_inspect_instance_mock_response()
    scenario_inspect_instance_spawn_location()
    scenario_inspect_instance_property_serialization()
    scenario_inspect_instance_validation()
    scenario_connect_hello_ping_pong()
    scenario_disconnect_and_reconnect()
    scenario_errors()
    scenario_create_part_success()
    scenario_create_part_physics_round_trip()
    scenario_create_part_failure()
    scenario_create_script_success()
    scenario_create_script_failure()
    scenario_modify_instance_success_roundtrip()
    scenario_modify_instance_failure()
    scenario_insert_asset_validation()
    scenario_insert_asset_known_ids_enforced()
    scenario_insert_asset_bounded_registry()
    scenario_insert_asset_success()
    scenario_insert_asset_failure()
    scenario_insert_asset_placement_modes()
    scenario_insert_asset_timeout()
    scenario_insert_asset_cli_rejects_usage()
    scenario_interactive_insert_asset_registered()
    scenario_inspect_hierarchy_roundtrip()
    scenario_find_instances_roundtrip()
    scenario_find_instances_roundtrip_truncated()
    scenario_find_instances_failure()
    scenario_inspect_instance_roundtrip()
    scenario_inspect_instance_not_found()
    scenario_interactive_create_part_registered()
    scenario_interactive_create_script_registered()
    scenario_interactive_modify_instance_registered()
    scenario_repl_run_after_connection()
    scenario_repl_after_plugin_connect_pty()
    print("\nAll protocol scenarios passed.")


if __name__ == "__main__":
    main()
