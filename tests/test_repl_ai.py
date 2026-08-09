#!/usr/bin/env python3
"""Interactive AI REPL tests (Phase 3C): plain text drives the AI agent.

These tests run cli/rbxforge.py as a subprocess and drive it like a user:
connect a fake plugin over WebSocket, type natural-language prompts at the
REPL, and watch the resulting tool request reach (and complete against) the
plugin. The provider is always the env-configured mock (RBXFORGE_MOCK_RESPONSE
/ RBXFORGE_MOCK_FAIL), so each scenario is deterministic and needs no model.

Reuses the WebSocket/plugin helpers from test_protocol.py (hello/welcome,
request/response, disconnect), so this file imports that module.

Run from the repository root:
    python3 tests/test_repl_ai.py
"""

import json
import os
import subprocess
import sys

from test_protocol import (
    CLI,
    HELLO,
    LineReader,
    Proc,
    connect_ws,
    recv_json,
    send_json,
    PROTOCOL_VERSION,
)

# --------------------------------------------------------------------------- #
# A Proc that runs the CLI with environment overrides (the AI provider config)
# --------------------------------------------------------------------------- #


class EnvProc(Proc):
    """Like Proc but adds environment overrides before launching the child."""

    def __init__(self, env, *args):
        merged = dict(os.environ)
        merged.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, CLI] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=merged,
        )
        self.reader = LineReader(self.proc.stdout)


MOCK_ENV = {"RBXFORGE_PROVIDER": "mock", "RBXFORGE_MODEL": "mock-model"}


def mock_env(response=None, fail=None):
    env = dict(MOCK_ENV)
    if response is not None:
        env["RBXFORGE_MOCK_RESPONSE"] = response
    if fail is not None:
        env["RBXFORGE_MOCK_FAIL"] = fail
    return env


def send_prompt(proc, text):
    proc.proc.stdin.write(text.encode("utf-8") + b"\n")
    proc.proc.stdin.flush()


def part_call_json():
    return json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "RedCube",
            "position": {"x": 0, "y": 5, "z": 0},
            "size": {"x": 1, "y": 1, "z": 1},
            "color": "red",
        },
    })


def ok_response(id):
    return {
        "type": "response",
        "id": id,
        "version": PROTOCOL_VERSION,
        "timestamp": 0.0,
        "payload": {
            "ok": True,
            "result": {
                "name": "RedCube",
                "position": {"x": 0, "y": 5, "z": 0},
                "size": {"x": 1, "y": 1, "z": 1},
                "color": "red",
            },
        },
    }


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def scenario_natural_language_prompt_creates_part():
    """'create a red cube' at the prompt must reach Studio as a create_part tool
    call (via agent -> ToolRegistry -> protocol), and the ok result must be
    reported with an exit code 0 on quit."""
    proc = EnvProc(mock_env(response=part_call_json()), "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()
        assert proc.reader.wait_for("PLUGIN CONNECTED"), proc._output()

        send_prompt(proc, "create a red cube")

        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_part", req
        assert req["payload"]["params"]["name"] == "RedCube", req
        assert req["payload"]["params"]["color"] == "red", req
        send_json(sock, ok_response(req["id"]))

        assert proc.reader.wait_for("AI OK: called 'create_part'"), proc._output()

        sock.close()
        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  natural-language prompt -> AI -> create_part tool call -> Studio result")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_normal_commands_still_work():
    """help/status/ping/create_part must work exactly as before (no AI involved)."""
    proc = EnvProc(mock_env(response=part_call_json()), "--port", "0")
    try:
        host, port = proc.listening_addr()
        assert proc.reader.wait_for("Type 'help'"), proc._output()

        send_prompt(proc, "help")
        assert proc.reader.wait_for("any other input is sent to the AI agent"), proc._output()

        send_prompt(proc, "status")
        assert proc.reader.wait_for("no plugin connected"), proc._output()

        # ping without a plugin is the well-known graceful no-connection path.
        send_prompt(proc, "ping")
        assert proc.reader.wait_for("cannot ping: no plugin is connected"), proc._output()

        # create_part without a plugin is the well-known graceful path too.
        send_prompt(proc, "create_part")
        assert proc.reader.wait_for("cannot execute create_part"), proc._output()

        # None of the command output mentioned the AI agent failing.
        assert not proc.reader.contains("AI failed"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  help/status/ping/create_part still work as commands (no AI)")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_explicit_ask_command():
    """'ask <prompt>' must behave exactly like plain prompt text."""
    proc = EnvProc(mock_env(response=part_call_json()), "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()

        send_prompt(proc, "ask create a red cube")
        req = recv_json(sock)
        assert req["type"] == "request", req
        assert req["payload"]["tool"] == "create_part", req
        send_json(sock, ok_response(req["id"]))
        assert proc.reader.wait_for("AI OK: called 'create_part'"), proc._output()

        # 'ask' with no prompt is a graceful no-op.
        send_prompt(proc, "ask")
        assert proc.reader.wait_for("ask: no prompt given"), proc._output()

        sock.close()
        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  explicit 'ask <prompt>' runs the agent; bare 'ask' is a gentle hint")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_provider_failure_keeps_repl():
    """A provider failure must be reported concisely and the REPL must survive."""
    proc = EnvProc(mock_env(fail="timeout"), "--port", "0")
    try:
        proc.listening_addr()
        send_prompt(proc, "create a red cube")
        assert proc.reader.wait_for("AI failed: provider_error"), proc._output()
        assert proc.reader.contains("timed out"), proc._output()

        # REPL still works afterwards.
        send_prompt(proc, "status")
        assert proc.reader.wait_for("no plugin connected"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  provider failure logged as 'AI failed: provider_error'; REPL survives")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_malformed_ai_output_keeps_repl():
    """Malformed model output (empty mock response) must not crash the REPL."""
    proc = EnvProc(mock_env(), "--port", "0")  # no RBXFORGE_MOCK_RESPONSE -> ""
    try:
        proc.listening_addr()
        send_prompt(proc, "create a red cube")
        assert proc.reader.wait_for("AI failed: malformed_output"), proc._output()

        send_prompt(proc, "status")
        assert proc.reader.wait_for("no plugin connected"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  malformed AI output logged; REPL stays alive")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_unknown_and_invalid_tools_keeps_repl():
    """Unknown tools and schema-invalid arguments are rejected without a send."""
    unknown = json.dumps({"tool": "not_a_tool", "arguments": {}})
    proc = EnvProc(mock_env(response=unknown), "--port", "0")
    try:
        proc.listening_addr()
        send_prompt(proc, "do the impossible")
        assert proc.reader.wait_for("AI failed: unknown_tool"), proc._output()
        assert proc.reader.wait_for("not_a_tool"), proc._output()
        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  unknown tool rejected with 'AI failed: unknown_tool'")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()

    invalid = json.dumps({
        "tool": "create_part",
        "arguments": {"name": "", "position": {"x": 1}, "size": {}, "color": "blue"},
    })
    proc = EnvProc(mock_env(response=invalid), "--port", "0")
    try:
        proc.listening_addr()
        send_prompt(proc, "make a part")
        assert proc.reader.wait_for("AI failed: invalid_arguments"), proc._output()

        send_prompt(proc, "status")
        assert proc.reader.wait_for("no plugin connected"), proc._output()
        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  invalid arguments rejected with 'AI failed: invalid_arguments'")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def scenario_repl_usable_after_plugin_connection():
    """The REPL must stay usable through: plugin connect -> AI prompt ->
    plugin disconnect -> another AI prompt."""
    proc = EnvProc(mock_env(response=part_call_json()), "--port", "0")
    try:
        host, port = proc.listening_addr()
        sock = connect_ws(host, port)
        send_json(sock, HELLO)
        assert recv_json(sock)["type"] == "welcome", proc._output()
        assert proc.reader.wait_for("PLUGIN CONNECTED"), proc._output()

        # AI prompt while connected; the request must reach us.
        send_prompt(proc, "create a red cube")
        req = recv_json(sock)
        assert req["payload"]["tool"] == "create_part", req
        send_json(sock, ok_response(req["id"]))
        assert proc.reader.wait_for("AI OK: called 'create_part'"), proc._output()

        # Disconnect the plugin; logs must not kill the REPL.
        sock.sendall(b"\x88\x80" + os.urandom(4))
        sock.close()
        assert proc.reader.wait_for("PLUGIN DISCONNECTED"), proc._output()

        # Another AI prompt: now there is no plugin, so execution reports
        # the graceful no-connection failure - and the REPL stays alive.
        send_prompt(proc, "create a red cube")
        assert proc.reader.wait_for("AI failed: execution_failed"), proc._output()
        assert proc.reader.wait_for("no plugin is connected"), proc._output()

        send_prompt(proc, "status")
        assert proc.reader.wait_for("no plugin connected"), proc._output()

        rc = proc.quit()
        assert rc == 0, "exit code {}; output:\n{}".format(rc, proc._output())
        print("OK  REPL usable before/during/after plugin connect + disconnect (with AI)")
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()


def main():
    scenario_natural_language_prompt_creates_part()
    scenario_normal_commands_still_work()
    scenario_explicit_ask_command()
    scenario_provider_failure_keeps_repl()
    scenario_malformed_ai_output_keeps_repl()
    scenario_unknown_and_invalid_tools_keeps_repl()
    scenario_repl_usable_after_plugin_connection()
    print("\nAll interactive AI REPL scenarios passed.")


if __name__ == "__main__":
    main()