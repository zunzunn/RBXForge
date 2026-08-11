#!/usr/bin/env python3
"""Agent (Phase 3B) tests: prompt -> provider -> structured tool call -> ToolRegistry.

These tests import cli/agent.py in-process. The agent locates its sibling
modules (cli/providers.py, cli/rbxforge.py) by adding its own directory to
sys.path, so no plugin or network is required. The provider is always the
deterministic MockProvider (or a small recording subclass), and tool execution
goes through a fake RBXForge connection whose send_request returns canned
plugin responses - so the whole pipeline is exercised deterministically.

Run from the repository root:
    python3 tests/test_agent.py
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "cli", "agent.py")

_AGENT_MODULE = None
providers = None
rbxforge = None


def load_agent_module():
    global _AGENT_MODULE
    global providers
    global rbxforge
    if _AGENT_MODULE is None:
        spec = importlib.util.spec_from_file_location("rbxforge_agent_mod", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _AGENT_MODULE = module
        providers = module.providers
        rbxforge = module.rbxforge
    return _AGENT_MODULE


# Load the agent module once (imports cli/providers.py and cli/rbxforge.py at
# module scope) so the test doubles below can subclass its classes.
load_agent_module()


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeRBX:
    """Stand-in for the RBXForge connection given to the ToolRegistry.

    ``response_payload`` is what ``send_request`` returns (the plugin's reply).
    Every request is recorded so tests can prove execution went through the tool
    layer (``create_part``'s ``run`` calls ``send_request`` with the validated
    params, and logs via ``log``).
    """

    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.requests = []
        self.logs = []

    def send_request(self, tool, params, timeout):
        self.requests.append((tool, params))
        return self.response_payload

    def log(self, message):
        self.logs.append(message)


class RecordingProvider(providers.MockProvider):
    """MockProvider that also records the messages sent to chat."""

    def __init__(self, response_text, **kwargs):
        super().__init__(response_text=response_text, **kwargs)
        self.chat_messages = None
        self.chat_options = None

    def chat(self, messages, **options):
        self.chat_messages = messages
        self.chat_options = options
        return super().chat(messages, **options)


def ok_part_response():
    """A plugin 'ok:true' response for create_part (matches the tool's expect)."""
    return {
        "ok": True,
        "result": {
            "name": "AgentCube",
            "position": {"x": 1, "y": 2, "z": 3},
            "size": {"x": 2, "y": 2, "z": 2},
            "color": "red",
        },
    }


def valid_part_arguments():
    return {
        "name": "AgentCube",
        "position": {"x": 1, "y": 2, "z": 3},
        "size": {"x": 2, "y": 2, "z": 2},
        "color": "red",
    }


def make_agent(provider, registry=None, rbx=None):
    mod = load_agent_module()
    return mod.Agent(provider, registry=registry, rbx=rbx)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def scenario_tool_definitions_sent_to_ai():
    """The agent must give the model the currently registered tool definitions."""
    mod = load_agent_module()
    provider = RecordingProvider("")
    agent = make_agent(provider)

    defs = agent.tool_definitions()
    names = [entry["name"] for entry in defs]
    assert names == ["create_part", "find_instances", "inspect_hierarchy"], names
    create_part = defs[0]
    assert isinstance(create_part["description"], str) and create_part["description"]
    assert create_part["parameters"]["type"] == "object"
    assert set(create_part["parameters"]["required"]) == {
        "name", "position", "size", "color",
    }, create_part
    hierarchy = next(d for d in defs if d["name"] == "inspect_hierarchy")
    assert hierarchy["parameters"]["properties"]["depth"]["type"] == "number"
    assert hierarchy["parameters"]["properties"]["depth"]["minimum"] == 1, hierarchy

    finder = next(d for d in defs if d["name"] == "find_instances")
    assert finder["parameters"]["required"] == ["query"], finder
    assert finder["parameters"]["properties"]["query"]["type"] == "string"
    assert finder["parameters"]["properties"]["max_results"]["type"] == "number"
    assert finder["parameters"]["properties"]["max_results"]["maximum"] == 100, finder

    result = agent.run("create a red cube")
    assert result.ok is False  # provider produced no output, but messages were sent
    messages = provider.chat_messages
    assert messages is not None
    assert messages[0]["role"] == "system"
    assert "create_part" in messages[0]["content"], messages[0]
    assert '"parameters"' in messages[0]["content"], messages[0]
    assert '"tool"' in messages[0]["content"], messages[0]
    assert messages[1] == {"role": "user", "content": "create a red cube"}
    print("OK  agent sends current tool definitions (name/description/schema) to the AI")


def scenario_valid_ai_tool_call():
    """A valid structured tool call must be parsed, validated, and executed,
    returning ok:true with the tool execution output."""
    mod = load_agent_module()
    provider = RecordingProvider(json.dumps({
        "tool": "create_part",
        "arguments": valid_part_arguments(),
    }))
    rbx = FakeRBX(ok_part_response())
    agent = make_agent(provider, rbx=rbx)

    result = agent.run("create a red cube")
    assert result.ok is True, result
    assert result.error is None, result
    assert result.tool.name == "create_part", result
    assert result.output is True, result
    assert rbx.requests == [("create_part", valid_part_arguments())], rbx.requests
    print("OK  valid AI tool call executed through the ToolRegistry (create_part ok)")


def scenario_unknown_tool_rejected():
    """An unknown tool name must be rejected safely (no execution, no crash)."""
    mod = load_agent_module()
    provider = RecordingProvider(json.dumps({
        "tool": "does_not_exist",
        "arguments": {},
    }))
    rbx = FakeRBX(ok_part_response())
    agent = make_agent(provider, rbx=rbx)

    result = agent.run("do something impossible")
    assert result.ok is False, result
    assert result.error["code"] == "unknown_tool", result
    assert "does_not_exist" in result.error["message"], result
    assert rbx.requests == [], rbx.requests  # never sent to the plugin
    print("OK  unknown tool rejected safely with error code 'unknown_tool'")


def scenario_invalid_arguments_rejected():
    """Schema-invalid arguments must be rejected by the ToolRegistry (no send)."""
    mod = load_agent_module()
    provider = RecordingProvider(json.dumps({
        "tool": "create_part",
        "arguments": {"name": "", "position": {"x": 1}, "size": {}, "color": "blue"},
    }))
    rbx = FakeRBX(ok_part_response())
    agent = make_agent(provider, rbx=rbx)

    result = agent.run("make a part")
    assert result.ok is False, result
    assert result.error["code"] == "invalid_arguments", result
    assert rbx.requests == [], rbx.requests  # invalid args never sent
    print("OK  invalid tool arguments rejected by the ToolRegistry with 'invalid_arguments'")


def scenario_malformed_model_output():
    """Malformed model output must be reported safely as 'malformed_output'."""
    mod = load_agent_module()
    bad_outputs = [
        "",                               # no output at all
        "I will create a cube!",          # no JSON
        "not {json",
        "[1, 2, 3]",                      # JSON but not an object
        '{"tool": 42, "arguments": {}}',  # tool not a string
        '{"arguments": {"x": 1}}',        # missing tool
        '{"tool": "create_part"}',        # missing arguments
        '{"tool": "create_part", "arguments": "nope"}',  # arguments not an object
    ]
    for text in bad_outputs:
        provider = RecordingProvider(text)
        rbx = FakeRBX(ok_part_response())
        agent = make_agent(provider, rbx=rbx)
        result = agent.run("create a cube")
        assert result.ok is False, result
        assert result.error["code"] == "malformed_output", result
        assert rbx.requests == [], rbx.requests
    print("OK  malformed model output reported safely as 'malformed_output' (8 cases)")


def scenario_fenced_json_accepted():
    """The parser must accept a JSON object inside a ```json fenced block."""
    mod = load_agent_module()
    text = 'Here you go:\n```json\n{"tool": "create_part", "arguments": ' + \
        json.dumps(valid_part_arguments()) + '}\n```\nDone.'
    provider = RecordingProvider(text)
    rbx = FakeRBX(ok_part_response())
    agent = make_agent(provider, rbx=rbx)
    result = agent.run("create a red cube")
    assert result.ok is True, result
    assert result.tool.name == "create_part", result
    print("OK  fenced JSON tool call is parsed and executed")


def scenario_provider_error():
    """Provider failures must surface as a safe 'provider_error' result."""
    mod = load_agent_module()
    for fail_mode, expected_type in (
        ("timeout", "ProviderTimeoutError"),
        ("connection", "ProviderConnectionError"),
        ("response", "ProviderResponseError"),
    ):
        provider = providers.MockProvider(fail=fail_mode)
        agent = make_agent(provider)
        result = agent.run("create a red cube")
        assert result.ok is False, result
        assert result.error["code"] == "provider_error", result
        assert result.error["type"] == expected_type, result
    print("OK  provider errors (timeout/connection/response) surface safely")


def scenario_create_part_through_registry_success():
    """The full happy path end-to-end: model call -> ToolRegistry -> tool -> ok.

    Uses the agent's default registry (the real ToolRegistry with create_part)
    and proves the execution flowed through it: the create_part tool's ``run``
    called the fake connection's ``send_request`` with the validated params, and
    the plugin's ok response produced output True.
    """
    mod = load_agent_module()
    provider = RecordingProvider(json.dumps({
        "tool": "create_part",
        "arguments": valid_part_arguments(),
    }))
    rbx = FakeRBX(ok_part_response())
    agent = make_agent(provider, rbx=rbx)

    registry = agent.registry
    assert isinstance(registry, mod.rbxforge.ToolRegistry), registry
    assert registry.get("create_part") is not None

    result = agent.run("create a red cube")
    assert result.ok is True, result
    assert result.output is True, result
    assert rbx.requests == [("create_part", valid_part_arguments())], rbx.requests
    assert any("create_part OK" in line for line in rbx.logs), rbx.logs
    print("OK  successful create_part executed through the existing ToolRegistry")


def scenario_agent_from_env_mock():
    """agent_from_env must build an agent from the environment (mock provider)."""
    mod = load_agent_module()
    saved = {key: os.environ.get(key) for key in ("RBXFORGE_PROVIDER", "RBXFORGE_MODEL")}
    try:
        os.environ["RBXFORGE_PROVIDER"] = "mock"
        os.environ["RBXFORGE_MODEL"] = "mock-model"
        agent = mod.agent_from_env()
        assert agent.provider.name == "mock", agent.provider
        assert agent.registry.get("create_part") is not None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("OK  agent_from_env builds an agent from environment configuration")


def scenario_regression_vec3_format_guidance():
    """Regression: real Ollama picked create_part but wrote position in a
    non-object format (observed: 'params.position must be an object with numeric
    x, y, z'). The model-facing definition must describe vectors as explicit
    objects so the model stops guessing, while validation stays strict."""

    # (1) What the AI now sees: vec3 shorthand (_model_schema) is flattened into
    # an explicit object schema with numeric x/y/z, so the model knows the shape.
    mod = load_agent_module()
    agent = make_agent(RecordingProvider(""))
    create_part = agent.tool_definitions()[0]
    vec = create_part["parameters"]["properties"]["position"]
    assert vec["type"] == "object", vec
    assert vec["required"] == ["x", "y", "z"], vec
    assert vec["properties"] == {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "z": {"type": "number"},
    }, vec
    assert create_part["parameters"]["properties"]["size"]["type"] == "object"
    prompt_text = mod.build_system_prompt(agent.registry)
    assert "never arrays like [0,5,0]" in prompt_text, prompt_text
    assert '"position": {"x": 0, "y": 5, "z": 0}' in prompt_text, prompt_text

    # (2) The observed model output shapes (position/size as non-objects) are
    # STILL rejected with exactly the reported error - validation is not
    # weakened by the representation fix.
    observed_shapes = {
        "array": {"position": [0, 5, 0], "size": [4, 4, 4]},
        "string": {"position": "0, 5, 0", "size": "4, 4, 4"},
    }
    for label, vectors in observed_shapes.items():
        arguments = dict(valid_part_arguments())
        arguments.update({"position": vectors["position"], "size": vectors["size"]})
        provider = RecordingProvider(json.dumps({
            "tool": "create_part",
            "arguments": arguments,
        }))
        rbx = FakeRBX(ok_part_response())
        result = make_agent(provider, rbx=rbx).run("create a red cube")
        assert result.ok is False, (label, result)
        assert result.error["code"] == "invalid_arguments", (label, result)
        assert "params.position must be an object with numeric x, y, z" in result.error["message"], \
            (label, result.error)
        assert rbx.requests == [], (label, rbx.requests)

    # (3) A compliant model (object form) still executes end-to-end.
    provider = RecordingProvider(json.dumps({
        "tool": "create_part",
        "arguments": valid_part_arguments(),
    }))
    rbx = FakeRBX(ok_part_response())
    result = make_agent(provider, rbx=rbx).run("create a red cube")
    assert result.ok is True, result
    print("OK  vec3 shown as explicit object schema; observed bad shapes still rejected; "
          "compliant calls still execute")


def main():
    scenario_tool_definitions_sent_to_ai()
    scenario_valid_ai_tool_call()
    scenario_unknown_tool_rejected()
    scenario_invalid_arguments_rejected()
    scenario_malformed_model_output()
    scenario_fenced_json_accepted()
    scenario_provider_error()
    scenario_create_part_through_registry_success()
    scenario_agent_from_env_mock()
    scenario_regression_vec3_format_guidance()
    print("\nAll agent scenarios passed.")


if __name__ == "__main__":
    main()