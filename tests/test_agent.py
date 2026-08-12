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
    assert names == ["create_part", "find_instances", "inspect_hierarchy", "inspect_instance"], names
    create_part = defs[0]
    assert isinstance(create_part["description"], str) and create_part["description"]
    assert create_part["parameters"]["type"] == "object"
    assert set(create_part["parameters"]["required"]) == {
        "name", "position", "size", "color",
    }, create_part
    assert create_part["parameters"]["properties"]["color"] == {
        "type": "string",
        "enum": ["red", "blue", "green", "yellow", "white", "black", "gray"],
    }, create_part
    # Phase 5B physics flags are exposed to the model automatically through the
    # schema conversion, with their CLI defaults and marked optional (not listed
    # in `required`).
    assert create_part["parameters"]["properties"]["anchored"] == {
        "type": "boolean", "default": True,
    }, create_part
    assert create_part["parameters"]["properties"]["can_collide"] == {
        "type": "boolean", "default": True,
    }, create_part
    assert create_part["parameters"]["properties"]["material"] == {
        "type": "string",
        "enum": [
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
        ],
        "default": "Plastic",
    }, create_part
    assert create_part["parameters"]["required"] == ["name", "position", "size", "color"], \
        create_part
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
        "arguments": {"name": "", "position": {"x": 1}, "size": {}, "color": "purple"},
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


class SequenceProvider(providers.MockProvider):
    """Deterministic provider for multi-step scenarios: returns scripted replies
    in order and records every chat call (messages passed). When the script runs
    out the next call raises ProviderResponseError, so a test proves the loop
    did not keep calling the provider past its budget."""

    def __init__(self, responses):
        super().__init__(response_text="", model="mock-model")
        self.responses = list(responses)
        self.chat_calls = []

    def chat(self, messages, **options):
        self.chat_calls.append((messages, options))
        if not self.responses:
            raise providers.ProviderResponseError("no scripted response left")
        text = self.responses.pop(0)
        return providers.ProviderResponse(
            text=text, model=self.model, provider=self.name, raw={"sequence": True}
        )


class MultiFakeRBX:
    """Fake RBXForge connection that returns a per-tool plugin payload and
    records every request/log line (the multi-step counterpart to FakeRBX)."""

    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.requests = []
        self.logs = []

    def send_request(self, tool, params, timeout):
        self.requests.append((tool, params))
        return self.payloads.get(tool)

    def log(self, message):
        self.logs.append(message)


def find_payload(query="Baseplate", total=1, matches=None, max_results=20):
    """A plugin 'ok:true' find_instances response payload."""
    if matches is None:
        matches = [
            {"name": query, "className": "Part", "path": "Workspace/" + query}
        ] * total
    return {
        "ok": True,
        "result": {
            "query": query,
            "max_results": max_results,
            "total": total,
            "count": len(matches),
            "truncated": total > len(matches),
            "matches": matches,
        },
    }


def inspect_payload(name="Baseplate", properties=None):
    """A plugin 'ok:true' inspect_instance response payload."""
    return {
        "ok": True,
        "result": {
            "name": name,
            "className": "Part",
            "path": "Workspace/" + name,
            "parent_path": "Workspace",
            "properties": properties or {"Anchored": True, "Size": {"x": 8, "y": 1, "z": 8}},
        },
    }


def find_call(query="Baseplate"):
    return json.dumps({"tool": "find_instances", "arguments": {"query": query}})


def inspect_call(path="Workspace.Baseplate"):
    return json.dumps({"tool": "inspect_instance", "arguments": {"path": path}})


def part_call():
    return json.dumps({"tool": "create_part", "arguments": valid_part_arguments()})


# --------------------------------------------------------------------------- #
# Phase 4D: bounded multi-step loop scenarios
# --------------------------------------------------------------------------- #


def scenario_multistep_find_then_inspect_then_report():
    """The model must be able to call find_instances, receive its bounded
    result, call inspect_instance using that context, and finish with a report."""
    mod = load_agent_module()
    provider = SequenceProvider([
        find_call("Baseplate"),
        inspect_call("Workspace.Baseplate"),
        json.dumps({"message": "Baseplate is the island floor; no changes needed."}),
    ])
    rbx = MultiFakeRBX({
        "find_instances": find_payload(query="Baseplate", total=1),
        "inspect_instance": inspect_payload("Baseplate"),
    })
    agent = make_agent(provider, rbx=rbx)

    result = agent.run("check what is at the spawn and report")
    assert result.ok is True, result
    assert result.error is None, result
    assert result.tool is None, result
    assert "no changes needed" in result.message, result
    assert [step["tool"] for step in result.steps] == [
        "find_instances", "inspect_instance",
    ], result.steps

    # Both tools went through the ToolRegistry to the (fake) connection.
    assert rbx.requests == [
        ("find_instances", {"query": "Baseplate", "max_results": 20}),
        ("inspect_instance", {"path": "Workspace.Baseplate"}),
    ], rbx.requests

    # The find_instances result was fed back to the model on the next turn:
    # the second chat call carries the assistant's call plus a user message with
    # "Tool call #1: find_instances" and the bounded result content.
    calls = provider.chat_calls
    assert len(calls) == 3, len(calls)
    tool_result_messages = [
        m for m in calls[1][0] if m["role"] == "user" and m["content"].startswith("Tool call #")
    ]
    assert tool_result_messages, calls[1][0]
    assert "find_instances" in tool_result_messages[0]["content"], tool_result_messages[0]
    assert '"Baseplate"' in tool_result_messages[0]["content"], tool_result_messages[0]
    print("OK  multi-step: find_instances -> bounded result -> inspect_instance -> report")


def scenario_multistep_inspect_then_create_part():
    """The model must be able to inspect an object and then execute create_part
    using the gathered context; the loop stops after the action tool."""
    mod = load_agent_module()
    provider = SequenceProvider([
        inspect_call("Workspace/Shop"),
        part_call(),
    ])
    rbx = MultiFakeRBX({
        "inspect_instance": inspect_payload("Shop", properties={"PrimaryPart": "Workspace/Shop/Main"}),
        "create_part": ok_part_response(),
    })
    agent = make_agent(provider, rbx=rbx)

    result = agent.run("inspect the shop then add a floor part")
    assert result.ok is True, result
    assert result.tool.name == "create_part", result
    assert result.output is True, result
    assert result.message is None, result
    assert [step["tool"] for step in result.steps] == ["inspect_instance", "create_part"], \
        result.steps

    assert rbx.requests == [
        ("inspect_instance", {"path": "Workspace/Shop"}),
        ("create_part", valid_part_arguments()),
    ], rbx.requests

    # Three chat calls happened (inspect -> result -> action) and no more: the
    # loop does not ask for a summary after an action tool succeeds.
    assert len(provider.chat_calls) == 2, len(provider.chat_calls)
    print("OK  multi-step: inspect_instance -> create_part (single requests preserved)")


def scenario_multistep_single_call_still_single_step():
    """A model that answers a simple prompt with one action call must behave
    exactly like the Phase 3B single-step agent (one chat, one request)."""
    mod = load_agent_module()
    provider = SequenceProvider([part_call()])
    rbx = MultiFakeRBX({"create_part": ok_part_response()})
    result = make_agent(provider, rbx=rbx).run("create a red cube")
    assert result.ok is True, result
    assert result.tool.name == "create_part", result
    assert result.output is True, result
    assert len(provider.chat_calls) == 1, len(provider.chat_calls)
    assert rbx.requests == [("create_part", valid_part_arguments())], rbx.requests
    assert [step["tool"] for step in result.steps] == ["create_part"], result.steps
    print("OK  simple single-step request uses exactly one chat + one tool call")


def scenario_groq_compat_agent_passes_tools():
    """Agent-side half of the GPT-OSS/Groq compatibility fix: when the provider
    advertises ``supports_tools`` (Groq), the agent hands it the registry's tool
    definitions as ``tools`` chat options so Groq never defaults ``tool_choice``
    to "none"; and a provider returning JSON-in-text (translated from a native
    tool call) drives the multi-step loop unchanged. Providers without
    ``supports_tools`` (Ollama/mock) never receive ``tools``."""
    mod = load_agent_module()

    class GroqLikeProvider(SequenceProvider):
        supports_tools = True

    provider = GroqLikeProvider([
        json.dumps({"tool": "find_instances",
                    "arguments": {"query": "SpawnLocation", "max_results": 10}}),
        json.dumps({"message": "found SpawnLocation"}),
    ])
    rbx = MultiFakeRBX({"find_instances": find_payload(query="SpawnLocation")})
    result = make_agent(provider, rbx=rbx).run("find SpawnLocation")
    assert result.ok is True, result
    assert result.message == "found SpawnLocation", result
    assert result.steps[0]["tool"] == "find_instances", result.steps
    assert result.steps[0]["ok"] is True, result.steps
    assert rbx.requests == [
        ("find_instances", {"query": "SpawnLocation", "max_results": 10}),
    ], rbx.requests

    chat_options = provider.chat_calls[0][1]
    tools = chat_options.get("tools")
    assert isinstance(tools, list) and len(tools) == 4, tools
    names = [tool["name"] for tool in tools]
    assert names == ["create_part", "find_instances", "inspect_hierarchy", "inspect_instance"], \
        names
    # The definitions are the model-facing JSON Schema (vec3 flattened), exactly
    # what Groq's `tools` parameter accepts.
    create_part = tools[0]
    assert create_part["description"], create_part
    assert create_part["parameters"]["type"] == "object", create_part
    assert create_part["parameters"]["properties"]["position"]["type"] == "object", \
        create_part
    assert chat_options["tools"] is not None

    # Non-tool-calling providers (no `supports_tools` attribute -> Ollama, mock)
    # must receive exactly the same call as before: no `tools` key at all.
    provider2 = RecordingProvider(json.dumps({
        "tool": "create_part", "arguments": valid_part_arguments(),
    }))
    rbx2 = MultiFakeRBX({"create_part": ok_part_response()})
    result2 = make_agent(provider2, rbx=rbx2).run("create a red cube")
    assert result2.ok is True, result2
    assert "tools" not in provider2.chat_options, provider2.chat_options
    print("OK  Agent passes tool definitions to Groq (supports_tools) and stays JSON-in-text")


def scenario_multistep_final_message_without_tools():
    """A model that decides nothing needs to change completes successfully with
    a report and executes nothing."""
    mod = load_agent_module()
    provider = SequenceProvider([json.dumps({"message": "The project is already empty."})])
    rbx = MultiFakeRBX({})
    result = make_agent(provider, rbx=rbx).run("is there anything to clean up?")
    assert result.ok is True, result
    assert result.message == "The project is already empty.", result
    assert result.tool is None, result
    assert result.steps == [], result.steps
    assert rbx.requests == [], rbx.requests
    print("OK  model final report completes without executing any tool")


def scenario_max_tool_calls_enforced():
    """The loop must never exceed max_tool_calls executed tools per request.
    Five successful inspection calls exhaust the budget and yield a clear
    'max_tool_calls' failure (no 6th chat, no guessing)."""
    mod = load_agent_module()
    responses = [find_call("Baseplate")] * 5
    provider = SequenceProvider(responses)
    rbx = MultiFakeRBX({"find_instances": find_payload(query="Baseplate", total=1)})
    agent = make_agent(provider, rbx=rbx)

    result = agent.run("keep searching")
    assert result.ok is False, result
    assert result.error["code"] == "max_tool_calls", result
    assert len(result.steps) == 5, result.steps
    assert all(step["tool"] == "find_instances" for step in result.steps), result.steps
    # Exactly 5 chat calls happened; the provider has no 6th response to give.
    assert len(provider.chat_calls) == 5, len(provider.chat_calls)
    assert len(rbx.requests) == 5, rbx.requests
    assert result.provider_text is not None
    print("OK  max 5 tool calls enforced; budget exhaustion is a clear failure")


def scenario_max_tool_calls_configurable():
    """max_tool_calls is configurable on the Agent, and exploration is bounded
    by that value rather than a hard-coded number."""
    mod = load_agent_module()
    provider = SequenceProvider([inspect_call("Workspace/Shop")] * 3)
    rbx = MultiFakeRBX({"inspect_instance": inspect_payload("Shop")})
    agent = make_agent(provider, rbx=rbx)
    agent.max_tool_calls = 3

    result = agent.run("explore the shop")
    assert result.ok is False, result
    assert result.error["code"] == "max_tool_calls", result
    assert len(result.steps) == 3, result.steps
    assert len(provider.chat_calls) == 3, len(provider.chat_calls)
    print("OK  per-request tool-call budget is configurable")


def scenario_unknown_tool_in_multistep():
    """An unknown tool called mid-loop is rejected safely before any request is
    sent, and the loop stops instead of guessing."""
    mod = load_agent_module()
    provider = SequenceProvider([
        find_call("Shop"),
        json.dumps({"tool": "not_a_tool", "arguments": {}}),
    ])
    rbx = MultiFakeRBX({"find_instances": find_payload(query="Shop", total=1)})
    result = make_agent(provider, rbx=rbx).run("find the shop then do something odd")
    assert result.ok is False, result
    assert result.error["code"] == "unknown_tool", result
    # Only the valid first call reached the connection; the invalid one never did.
    assert rbx.requests == [("find_instances", {"query": "Shop", "max_results": 20})], \
        rbx.requests
    assert [step["tool"] for step in result.steps] == ["find_instances", "not_a_tool"], \
        result.steps
    assert result.steps[1]["ok"] is False, result.steps[1]
    print("OK  unknown tool mid-loop rejected before execution; loop stops")


def scenario_invalid_arguments_mid_loop():
    """Schema-invalid arguments mid-loop are rejected with no request sent."""
    mod = load_agent_module()
    provider = SequenceProvider([
        inspect_call("Workspace/Shop"),
        json.dumps({
            "tool": "create_part",
            "arguments": {"name": "", "position": {"x": 1}, "size": {}, "color": "purple"},
        }),
    ])
    rbx = MultiFakeRBX({"inspect_instance": inspect_payload("Shop")})
    result = make_agent(provider, rbx=rbx).run("inspect then make an invalid part")
    assert result.ok is False, result
    assert result.error["code"] == "invalid_arguments", result
    assert rbx.requests == [("inspect_instance", {"path": "Workspace/Shop"})], rbx.requests
    assert [step["tool"] for step in result.steps] == ["inspect_instance", "create_part"], \
        result.steps
    assert result.steps[1]["ok"] is False, result.steps[1]
    print("OK  invalid arguments mid-loop rejected by the ToolRegistry")


def scenario_provider_error_mid_loop():
    """A provider failure after a successful inspection is a safe 'provider_error'
    (the inspection context is preserved in steps for diagnostics)."""
    mod = load_agent_module()
    provider = SequenceProvider([find_call("Shop")])   # fails on the 2nd chat call
    rbx = MultiFakeRBX({"find_instances": find_payload(query="Shop", total=1)})
    result = make_agent(provider, rbx=rbx).run("find the shop")
    assert result.ok is False, result
    assert result.error["code"] == "provider_error", result
    assert result.error["type"] == "ProviderResponseError", result
    assert [step["tool"] for step in result.steps] == ["find_instances"], result.steps
    print("OK  provider failure mid-loop surfaces as provider_error (no crash)")


def scenario_malformed_output_mid_loop():
    """Malformed model output after a successful inspection is reported safely."""
    mod = load_agent_module()
    provider = SequenceProvider([
        find_call("Shop"),
        "I will now do the thing!",
    ])
    rbx = MultiFakeRBX({"find_instances": find_payload(query="Shop", total=1)})
    result = make_agent(provider, rbx=rbx).run("find the shop and proceed")
    assert result.ok is False, result
    assert result.error["code"] == "malformed_output", result
    assert len(result.steps) == 1, result.steps
    print("OK  malformed model output mid-loop reported as malformed_output")


def scenario_bounded_result_never_exposed_unbounded():
    """Results fed back to the model must be bounded even when the plugin's own
    bounds were generous: large match lists are capped and strings truncated,
    and the serialized text respects the character budget."""
    mod = load_agent_module()
    many_matches = [
        {"name": "Match {0}".format(i), "className": "Part",
         "path": "Workspace/LongNames/Match {0}/".format(i) + "x" * 500}
        for i in range(200)
    ]
    provider = SequenceProvider([
        find_call("Match"),
        json.dumps({"message": "surveyed."}),
    ])
    rbx = MultiFakeRBX({
        "find_instances": find_payload(query="Match", total=200, matches=many_matches),
    })
    result = make_agent(provider, rbx=rbx).run("survey the matches")

    step = result.steps[0]
    assert step["tool"] == "find_instances", step
    # The bounded structure (before final string truncation) caps the list.
    matches = step["data"]["result"]["matches"]
    assert len(matches) <= mod.MAX_TOOL_RESULT_ITEMS, len(matches)
    for match in matches:
        # 500-char paths were truncated at the per-string budget.
        assert len(match["path"]) <= mod.MAX_TOOL_RESULT_STRING + 3, match["path"]
    # The exact serialized result shown to the model respects the char budget.
    assert len(step["result"]) <= mod.MAX_TOOL_RESULT_CHARS, len(step["result"])

    # The same bounded payload is what the model saw on the next turn.
    assert "Tool call #1: find_instances" in provider.chat_calls[1][0][-1]["content"]
    print("OK  tool results shown to the model are tightly bounded (matches/string/chars)")


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
    scenario_multistep_find_then_inspect_then_report()
    scenario_multistep_inspect_then_create_part()
    scenario_multistep_single_call_still_single_step()
    scenario_groq_compat_agent_passes_tools()
    scenario_multistep_final_message_without_tools()
    scenario_max_tool_calls_enforced()
    scenario_max_tool_calls_configurable()
    scenario_unknown_tool_in_multistep()
    scenario_invalid_arguments_mid_loop()
    scenario_provider_error_mid_loop()
    scenario_malformed_output_mid_loop()
    scenario_bounded_result_never_exposed_unbounded()
    print("\nAll agent scenarios passed.")


if __name__ == "__main__":
    main()