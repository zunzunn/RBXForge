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

# Phase 9B: make the repository root importable so ``cli.intent`` can be
# imported directly (the agent bootstrap loads its siblings under different
# top-level module names).
sys.path.insert(0, ROOT)
import cli.intent as intent  # noqa: E402


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


def valid_script_arguments():
    return {
        "name": "AgentScript",
        "type": "Script",
        "parent_path": "ServerScriptService",
        "source": 'print("hi")\n',
    }


def ok_script_response():
    """A plugin 'ok:true' response for create_script (matches the tool's expect)."""
    return {
        "ok": True,
        "result": {
            "name": "AgentScript",
            "type": "Script",
            "parent_path": "ServerScriptService",
            "path": "ServerScriptService/AgentScript",
            "source_length": 13,
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
    assert names == ["analyze_scene", "asset_search", "build", "create_part",
                     "create_script", "decompose_intent", "delete_instance",
                     "edit_build", "find_instances", "insert_asset",
                     "inspect_hierarchy", "inspect_instance", "modify_instance",
                     "plan_build", "recent_build_context", "recommend_assets"], names
    asset_search = defs[1]
    assert isinstance(asset_search["description"], str) and asset_search["description"]
    assert asset_search["parameters"]["type"] == "object"
    assert set(asset_search["parameters"]["required"]) == {"query"}, asset_search
    assert set(asset_search["parameters"]["properties"]) == {
        "query", "asset_type", "max_results",
    }, asset_search
    recommend = [entry for entry in defs if entry["name"] == "recommend_assets"][0]
    assert isinstance(recommend["description"], str) and recommend["description"]
    assert recommend["parameters"]["type"] == "object"
    assert set(recommend["parameters"]["required"]) == {"query"}, recommend
    assert set(recommend["parameters"]["properties"]) == {
        "query", "asset_type", "creator", "max_results", "limit",
    }, recommend
    create_part = [entry for entry in defs if entry["name"] == "create_part"][0]
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
    create_script = next(d for d in defs if d["name"] == "create_script")
    assert isinstance(create_script["description"], str) and create_script["description"]
    assert create_script["parameters"]["type"] == "object"
    assert create_script["parameters"]["required"] == ["name"], create_script
    assert create_script["parameters"]["properties"]["type"] == {
        "type": "string",
        "enum": ["Script", "LocalScript", "ModuleScript"],
        "default": "Script",
    }, create_script
    assert create_script["parameters"]["properties"]["source"] == {
        "type": "string", "default": "",
    }, create_script

    modify = next(d for d in defs if d["name"] == "modify_instance")
    assert isinstance(modify["description"], str) and modify["description"]
    assert modify["parameters"]["type"] == "object"
    assert modify["parameters"]["required"] == ["path", "properties"], modify
    assert modify["parameters"]["properties"]["path"]["type"] == "string", modify
    inner = modify["parameters"]["properties"]["properties"]
    assert inner["type"] == "object", inner
    assert inner["additionalProperties"] is False, inner
    props = inner["properties"]
    assert props["position"]["type"] == "object", props
    assert props["transparency"]["type"] == "number", props
    assert props["transparency"]["minimum"] == 0 and props["transparency"]["maximum"] == 1, props
    assert props["duration"]["type"] == "number" and props["duration"]["minimum"] == 0, props
    assert props["color"]["enum"] == ["red", "blue", "green", "yellow", "white", "black", "gray"], props
    assert props["material"]["enum"] == [
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
    ], props
    assert "enabled" in props and "duration" in props and "neutral" in props, props
    assert props["team_color"]["type"] == "string", props

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
    assert "create_script" in messages[0]["content"], messages[0]
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
    create_part = [entry for entry in agent.tool_definitions() if entry["name"] == "create_part"][0]
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
    records every request/log line (the multi-step counterpart to FakeRBX).
    ``known_assets`` pre-seeds the Phase 7C known-id registry. Without it the
    insert_asset tool's known-id check (active through CapturingRBX) rejects
    every id, which is the correct invented-id protection."""

    def __init__(self, payloads, known_assets=None):
        self.payloads = dict(payloads)
        self.requests = []
        self.logs = []
        self.known_assets = dict(known_assets) if known_assets else {}
        self._recent_build_context = []

    def send_request(self, tool, params, timeout):
        self.requests.append((tool, params))
        if tool == "recent_build_context":
            return {"ok": True, "result": {"context": self._recent_build_context}}
        return self.payloads.get(tool)

    def log(self, message):
        self.logs.append(message)

    def set_recent_build_context(self, context):
        self._recent_build_context = list(context)

    def recent_build_context(self):
        return list(self._recent_build_context)

    def remember_assets(self, results):
        count = 0
        for entry in results or []:
            if isinstance(entry, dict) and entry.get("asset_id") is not None:
                self.known_assets.setdefault(str(entry["asset_id"]), entry)
                count += 1
        return count

    def asset_known(self, asset_id):
        return str(asset_id) in self.known_assets

    def known_asset(self, asset_id):
        return self.known_assets.get(str(asset_id))


class BuildFakeRBX(MultiFakeRBX):
    """MultiFakeRBX extended for Phase 8A build tests.

    ``verify_map`` returns a per-path inspect_instance payload so final
    verification can be scripted independently of the initial scene inspection.
    ``per_tool_counts`` is an ordered list of payloads for a tool so successive
    calls to the same tool can return different results (e.g. one create_part
    succeeds and the next fails).
    """

    def __init__(self, payloads, known_assets=None, verify_map=None,
                 per_tool_counts=None):
        super().__init__(payloads, known_assets=known_assets)
        self.verify_map = dict(verify_map) if verify_map else {}
        self.per_tool_counts = dict(per_tool_counts) if per_tool_counts else {}
        self._call_counts = {}

    def send_request(self, tool, params, timeout):
        self.requests.append((tool, params))
        if tool == "inspect_instance":
            path = params.get("path")
            if path in self.verify_map:
                return self.verify_map[path]
        payloads = self.per_tool_counts.get(tool)
        if payloads:
            index = self._call_counts.get(tool, 0)
            self._call_counts[tool] = index + 1
            if index < len(payloads):
                return payloads[index]
        return self.payloads.get(tool)


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


def script_call():
    return json.dumps({"tool": "create_script", "arguments": valid_script_arguments()})


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
    using the gathered context; the loop permits one optional inspect_instance
    verification step after the action tool (Phase 6B)."""
    mod = load_agent_module()
    provider = SequenceProvider([
        inspect_call("Workspace/Shop"),
        part_call(),
        json.dumps({"message": "Added a floor part to the shop."}),
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
    assert result.message == "Added a floor part to the shop.", result.message
    assert [step["tool"] for step in result.steps] == ["inspect_instance", "create_part"], \
        result.steps

    assert rbx.requests == [
        ("inspect_instance", {"path": "Workspace/Shop"}),
        ("create_part", valid_part_arguments()),
    ], rbx.requests

    # Three chat calls happened (inspect -> result -> action, then final report):
    # the loop permits one optional inspect_instance verification step after
    # the action tool succeeds, and the model completes with a final report.
    assert len(provider.chat_calls) == 3, len(provider.chat_calls)
    assert result.message == "Added a floor part to the shop.", result.message


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


def scenario_create_script_action_tool():
    """create_script must be an action tool (Phase 6): listed in ACTION_TOOLS,
    exposed to the model, and — like create_part — executing it ends the loop
    with a single chat call and no follow-up summary request."""
    mod = load_agent_module()
    assert "create_script" in mod.ACTION_TOOLS, mod.ACTION_TOOLS

    provider = SequenceProvider([script_call()])
    rbx = MultiFakeRBX({"create_script": ok_script_response()})
    result = make_agent(provider, rbx=rbx).run("add a server script that prints hi")

    assert result.ok is True, result
    assert result.error is None, result
    assert result.tool.name == "create_script", result
    assert result.output is True, result
    assert result.message is None, result
    assert [step["tool"] for step in result.steps] == ["create_script"], result.steps
    assert rbx.requests == [("create_script", valid_script_arguments())], rbx.requests
    assert len(provider.chat_calls) == 1, len(provider.chat_calls)
    assert any("create_script OK" in line for line in rbx.logs), rbx.logs
    print("OK  create_script is an action tool: executed once, loop stops, ok reported")

    # A create_script execution failure (plugin replies ok:false) must be
    # reported as execution_failed, not crash the loop.
    failing = ok_script_response()
    failing["ok"] = False
    failing["error"] = {"code": "not_found", "message": "parent not found"}
    rbx_fail = MultiFakeRBX({"create_script": failing})
    provider_fail = SequenceProvider([script_call()])
    result_fail = make_agent(provider_fail, rbx=rbx_fail).run("add a script")
    assert result_fail.ok is False, result_fail
    assert result_fail.error["code"] == "execution_failed", result_fail
    assert result_fail.steps[0]["tool"] == "create_script", result_fail.steps
    assert result_fail.steps[0]["ok"] is False, result_fail.steps
    print("OK  create_script failure in agent loop reported as execution_failed")


def scenario_insert_asset_action_tool():
    """insert_asset must be an action tool (Phase 7C) and Phase 7D must
    automatically verify the inserted instance at the reported path before
    reporting success. Verification failures are reported as
    verification_failed, not success."""
    mod = load_agent_module()
    assert "insert_asset" in mod.ACTION_TOOLS, mod.ACTION_TOOLS

    insert_result = {
        "ok": True,
        "result": {
            "asset_id": "135522", "name": "Cafe Shop", "class": "Model",
            "parent_path": "Workspace", "path": "Workspace/Cafe Shop",
            "positioned": True, "placement": "default",
            "position": {"x": 0, "y": 5, "z": 0},
        },
    }
    insert_call = json.dumps({
        "tool": "insert_asset",
        "arguments": {"asset_id": "135522"},
    })
    verify_payload = {
        "ok": True,
        "result": {
            "name": "Cafe Shop",
            "className": "Model",
            "path": "Workspace/Cafe Shop",
            "parent_path": "Workspace",
            "properties": {},
        },
    }

    # Successful insert: the agent automatically calls inspect_instance at the
    # reported path and only reports success after verification matches.
    provider = SequenceProvider([
        insert_call,
        json.dumps({"message": "inserted a cafe shop model into Workspace"}),
    ])
    rbx = MultiFakeRBX({
        "insert_asset": insert_result,
        "inspect_instance": verify_payload,
    }, known_assets={"135522": {"asset_id": "135522", "asset_type": "Model",
                                "name": "Cafe Shop"}})
    result = make_agent(provider, rbx=rbx).run("add a shop model near the SpawnLocation")

    assert result.ok is True, result
    assert result.error is None, result
    assert result.tool.name == "insert_asset", result
    assert result.message is not None, result
    assert "Verified" in result.message, result.message
    assert [step["tool"] for step in result.steps] == ["insert_asset", "inspect_instance"], result.steps
    assert rbx.requests == [
        ("insert_asset", {"asset_id": "135522"}),
        ("inspect_instance", {"path": "Workspace/Cafe Shop"}),
    ], rbx.requests
    # Only one provider chat is consumed: the agent auto-verifies and reports.
    assert len(provider.chat_calls) == 1, len(provider.chat_calls)
    assert any("insert_asset OK" in line for line in rbx.logs), rbx.logs
    print("OK  insert_asset is an action tool; automatic verification succeeds")

    # Duplicate-name handling: the plugin may rename to a unique suffix; the
    # agent verifies at the returned path and reports that path, not the
    # original name.
    duplicate_insert = dict(insert_result)
    duplicate_insert["result"] = dict(insert_result["result"])
    duplicate_insert["result"]["name"] = "Cafe Shop2"
    duplicate_insert["result"]["path"] = "Workspace/Cafe Shop2"
    duplicate_verify = {
        "ok": True,
        "result": {
            "name": "Cafe Shop2",
            "className": "Model",
            "path": "Workspace/Cafe Shop2",
            "parent_path": "Workspace",
            "properties": {},
        },
    }
    provider_dup = SequenceProvider([
        json.dumps({"tool": "insert_asset", "arguments": {"asset_id": "135522"}}),
        json.dumps({"message": "inserted a uniquely-named shop model"}),
    ])
    rbx_dup = MultiFakeRBX({
        "insert_asset": duplicate_insert,
        "inspect_instance": duplicate_verify,
    }, known_assets={"135522": {"asset_id": "135522", "asset_type": "Model"}})
    result_dup = make_agent(provider_dup, rbx=rbx_dup).run("add another shop model")
    assert result_dup.ok is True, result_dup
    assert "Cafe Shop2" in result_dup.message, result_dup
    assert [step["tool"] for step in result_dup.steps] == ["insert_asset", "inspect_instance"], result_dup.steps
    assert rbx_dup.requests[1] == ("inspect_instance", {"path": "Workspace/Cafe Shop2"}), rbx_dup.requests
    assert len(provider_dup.chat_calls) == 1, len(provider_dup.chat_calls)
    print("OK  insert_asset verification follows duplicate-name suffix from the plugin")

    # Verification failure: insert reports success but the instance is missing.
    provider_missing = SequenceProvider([
        insert_call,
        json.dumps({"message": "done"}),
    ])
    rbx_missing = MultiFakeRBX({
        "insert_asset": insert_result,
        "inspect_instance": {"ok": False, "error": {"code": "not_found", "message": "no instance at path"}},
    }, known_assets={"135522": {"asset_id": "135522", "asset_type": "Model"}})
    result_missing = make_agent(provider_missing, rbx=rbx_missing).run("add a shop model")
    assert result_missing.ok is False, result_missing
    assert result_missing.error["code"] == "verification_failed", result_missing
    assert [step["tool"] for step in result_missing.steps] == ["insert_asset", "inspect_instance"], result_missing.steps
    assert len(provider_missing.chat_calls) == 1, len(provider_missing.chat_calls)
    print("OK  insert_asset verification failure (missing instance) reported as verification_failed")

    # Verification failure: path/class mismatch.
    mismatch_payload = {
        "ok": True,
        "result": {
            "name": "Other Model", "className": "Part",
            "path": "Workspace/Cafe Shop", "properties": {},
        },
    }
    provider_mismatch = SequenceProvider([
        insert_call,
        json.dumps({"message": "done"}),
    ])
    rbx_mismatch = MultiFakeRBX({
        "insert_asset": insert_result,
        "inspect_instance": mismatch_payload,
    }, known_assets={"135522": {"asset_id": "135522", "asset_type": "Model"}})
    result_mismatch = make_agent(provider_mismatch, rbx=rbx_mismatch).run("add a shop model")
    assert result_mismatch.ok is False, result_mismatch
    assert result_mismatch.error["code"] == "verification_failed", result_mismatch
    assert len(provider_mismatch.chat_calls) == 1, len(provider_mismatch.chat_calls)
    print("OK  insert_asset verification catches name/class mismatch")

    # A plugin-side insert failure (ok:false) is reported as execution_failed
    # and never reaches verification.
    failing = dict(insert_result)
    failing["ok"] = False
    failing["error"] = {"code": "not_found", "message": "asset not found or not insertable: 999999"}
    rbx_fail = MultiFakeRBX({"insert_asset": failing})
    provider_fail = SequenceProvider([json.dumps({
        "tool": "insert_asset", "arguments": {"asset_id": "999999"},
    })])
    result_fail = make_agent(provider_fail, rbx=rbx_fail).run("insert a ghost asset")
    assert result_fail.ok is False, result_fail
    assert result_fail.error["code"] == "execution_failed", result_fail
    assert [step["tool"] for step in result_fail.steps] == ["insert_asset"], result_fail.steps
    assert result_fail.steps[0]["ok"] is False, result_fail.steps
    print("OK  insert_asset failure in agent loop reported as execution_failed")


def scenario_scene_aware_building():
    """Phase 8A: the model can declare a multi-object build, inspect the scene,
    execute a bounded sequence of create/insert actions, and have every created
    path verified before the build is reported complete. Partial failures and
    verification failures are reported as build_failed, not success."""
    mod = load_agent_module()
    assert "build" in [t.name for t in mod.rbxforge.default_registry().list()]

    spawn_payload = {
        "ok": True,
        "result": {
            "name": "SpawnLocation",
            "className": "SpawnLocation",
            "path": "Workspace/SpawnLocation",
            "parent_path": "Workspace",
            "properties": {"Position": {"x": 0, "y": 0, "z": 0}},
        },
    }

    def part_payload(name, path=None):
        return {
            "ok": True,
            "result": {
                "name": name,
                "parent_path": "Workspace",
                "path": path or "Workspace/" + name,
                "position": {"x": 5, "y": 0.5, "z": 0},
                "size": {"x": 2, "y": 2, "z": 2},
                "color": "gray",
            },
        }

    sign_payload = {
        "ok": True,
        "result": {
            "name": "ShopSign",
            "type": "Script",
            "parent_path": "ServerScriptService",
            "path": "ServerScriptService/ShopSign",
            "source_length": 20,
        },
    }

    asset_payload = {
        "ok": True,
        "result": {
            "asset_id": "135522",
            "name": "Cafe Shop",
            "class": "Model",
            "parent_path": "Workspace",
            "path": "Workspace/Cafe Shop",
            "positioned": True,
            "placement": "default",
        },
    }

    build_call = json.dumps({
        "tool": "build",
        "arguments": {
            "description": "small shop near the SpawnLocation",
            "reference_path": "Workspace.SpawnLocation",
        },
    })
    inspect_spawn = json.dumps({
        "tool": "inspect_instance",
        "arguments": {"path": "Workspace.SpawnLocation"},
    })
    create_floor = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopFloor",
            "position": {"x": 5, "y": 0.5, "z": 0},
            "size": {"x": 8, "y": 1, "z": 6},
            "color": "gray",
        },
    })
    create_wall = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopWall",
            "position": {"x": 5, "y": 3, "z": -3},
            "size": {"x": 8, "y": 5, "z": 1},
            "color": "gray",
        },
    })
    create_sign = json.dumps({
        "tool": "create_script",
        "arguments": {
            "name": "ShopSign",
            "type": "Script",
            "source": 'print("Open!")',
        },
    })
    insert_shop = json.dumps({
        "tool": "insert_asset",
        "arguments": {"asset_id": "135522"},
    })
    final_report = json.dumps({
        "message": "Built a small shop near the SpawnLocation.",
    })

    # 1) Successful mixed build: inspect scene, create two parts + script + asset,
    #    then final report. All paths verify.
    verify_ok = {
        "Workspace/ShopFloor": {
            "ok": True,
            "result": {
                "name": "ShopFloor",
                "className": "Part",
                "path": "Workspace/ShopFloor",
            },
        },
        "ServerScriptService/ShopSign": {
            "ok": True,
            "result": {
                "name": "ShopSign",
                "className": "Script",
                "path": "ServerScriptService/ShopSign",
            },
        },
        "Workspace/Cafe Shop": {
            "ok": True,
            "result": {
                "name": "Cafe Shop",
                "className": "Model",
                "path": "Workspace/Cafe Shop",
            },
        },
    }
    provider = SequenceProvider([
        build_call,
        inspect_spawn,
        create_floor,
        create_wall,
        create_sign,
        insert_shop,
        final_report,
    ])
    rbx = BuildFakeRBX(
        {
            "inspect_instance": spawn_payload,
            "create_part": part_payload("ShopFloor"),
            "create_script": sign_payload,
            "insert_asset": asset_payload,
        },
        known_assets={"135522": {"asset_id": "135522", "asset_type": "Model"}},
        verify_map=verify_ok,
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), part_payload("ShopWall")],
        },
    )
    result = make_agent(provider, rbx=rbx).run(
        "build a small shop near the SpawnLocation"
    )

    assert result.ok is True, result
    assert result.message == "Built a small shop near the SpawnLocation.", result
    assert [step["tool"] for step in result.steps] == [
        "build", "inspect_instance", "create_part", "create_part",
        "create_script", "insert_asset",
        "inspect_instance", "inspect_instance", "inspect_instance",
        "inspect_instance",
    ], [step["tool"] for step in result.steps]
    assert all(step["ok"] for step in result.steps), result.steps
    # Seven model-driven chats (build, inspect, four actions, final); verification
    # is automatic and not a chat turn.
    assert len(provider.chat_calls) == 7, len(provider.chat_calls)
    # Created paths tracked for final verification.
    assert rbx.requests[0] == (
        "inspect_instance", {"path": "Workspace.SpawnLocation"}
    ), rbx.requests
    assert rbx.requests[1] == ("create_part", {
        "name": "ShopFloor",
        "position": {"x": 5, "y": 0.5, "z": 0},
        "size": {"x": 8, "y": 1, "z": 6},
        "color": "gray",
    }), rbx.requests
    assert rbx.requests[2] == ("create_part", {
        "name": "ShopWall",
        "position": {"x": 5, "y": 3, "z": -3},
        "size": {"x": 8, "y": 5, "z": 1},
        "color": "gray",
    }), rbx.requests
    assert rbx.requests[3] == ("create_script", {
        "name": "ShopSign", "type": "Script", "source": 'print("Open!")',
    }), rbx.requests
    assert rbx.requests[4] == ("insert_asset", {"asset_id": "135522"}), rbx.requests
    assert rbx.requests[5:] == [
        ("inspect_instance", {"path": "Workspace/ShopFloor"}),
        ("inspect_instance", {"path": "Workspace/ShopWall"}),
        ("inspect_instance", {"path": "ServerScriptService/ShopSign"}),
        ("inspect_instance", {"path": "Workspace/Cafe Shop"}),
    ], rbx.requests
    print("OK  scene-aware build: inspect scene, plan, multi-object execute, verify")

    # 2) Partial failure: the floor succeeds but the wall fails. The loop
    #    continues, the model sends a final report, and the result is
    #    build_failed because one step failed.
    wall_fail = {"ok": False, "error": {"code": "execution_failed", "message": "no room"}}
    provider_partial = SequenceProvider([
        build_call,
        inspect_spawn,
        create_floor,
        create_wall,
        final_report,
    ])
    rbx_partial = BuildFakeRBX(
        {"inspect_instance": spawn_payload},
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), wall_fail],
        },
    )
    result_partial = make_agent(provider_partial, rbx=rbx_partial).run(
        "build a small shop near the SpawnLocation"
    )
    assert result_partial.ok is False, result_partial
    assert result_partial.error["code"] == "build_failed", result_partial
    assert any(step["tool"] == "create_part" and step["ok"] is False
               for step in result_partial.steps), result_partial.steps
    print("OK  scene-aware build reports build_failed when a step fails")

    # 3) Verification failure: all steps report success but one created path is
    #    missing during final verification. The build is not reported as complete.
    verify_missing = dict(verify_ok)
    verify_missing["Workspace/ShopFloor"] = {
        "ok": False,
        "error": {"code": "not_found", "message": "no instance"},
    }
    provider_verify = SequenceProvider([
        build_call,
        inspect_spawn,
        create_floor,
        create_wall,
        create_sign,
        insert_shop,
        final_report,
    ])
    rbx_verify = BuildFakeRBX(
        {
            "inspect_instance": spawn_payload,
            "create_part": part_payload("ShopFloor"),
            "create_script": sign_payload,
            "insert_asset": asset_payload,
        },
        known_assets={"135522": {"asset_id": "135522", "asset_type": "Model"}},
        verify_map=verify_missing,
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), part_payload("ShopWall")],
        },
    )
    result_verify = make_agent(provider_verify, rbx=rbx_verify).run(
        "build a small shop near the SpawnLocation"
    )
    assert result_verify.ok is False, result_verify
    assert result_verify.error["code"] == "build_failed", result_verify
    assert "Workspace/ShopFloor" in result_verify.error["message"], result_verify
    print("OK  scene-aware build reports build_failed when final verification fails")

    # 4) Tool-call budget: build mode must raise the effective budget enough to
    #    execute several action tools. Without build mode the first create_part
    #    would end the loop; here three action tools plus build/inspect fit.
    provider_budget = SequenceProvider([
        build_call,
        inspect_spawn,
        create_floor,
        create_wall,
        create_sign,
        final_report,
    ])
    rbx_budget = BuildFakeRBX(
        {
            "inspect_instance": spawn_payload,
            "create_script": sign_payload,
        },
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), part_payload("ShopWall")],
        },
    )
    # Default max_tool_calls is 5; this build needs 5 model-driven calls plus
    # verification, so it only succeeds if build mode raised the budget.
    result_budget = make_agent(provider_budget, rbx=rbx_budget).run(
        "build a small shop near the SpawnLocation"
    )
    assert result_budget.ok is True, result_budget
    assert len([s for s in result_budget.steps if s["tool"] == "create_part"]) == 2, result_budget.steps
    assert len([s for s in result_budget.steps if s["tool"] == "create_script"]) == 1, result_budget.steps
    print("OK  scene-aware build raises the tool-call budget in build mode")


def scenario_intelligent_build_planning():
    """Phase 8B: the model submits a structured, bounded plan via plan_build
    inside build mode. The Agent validates the plan, tracks execution, skips
    redundant scene inspections, and produces a clear natural-language summary
    of what was built. Invalid plans and partial failures are reported as
    build failures."""
    mod = load_agent_module()
    registry = mod.rbxforge.default_registry()
    assert "plan_build" in [t.name for t in registry.list()]

    spawn_payload = {
        "ok": True,
        "result": {
            "name": "SpawnLocation",
            "className": "SpawnLocation",
            "path": "Workspace/SpawnLocation",
            "parent_path": "Workspace",
            "properties": {"Position": {"x": 0, "y": 0, "z": 0}},
        },
    }

    def part_payload(name, path=None):
        return {
            "ok": True,
            "result": {
                "name": name,
                "parent_path": "Workspace",
                "path": path or "Workspace/" + name,
                "position": {"x": 5, "y": 0.5, "z": 0},
                "size": {"x": 2, "y": 2, "z": 2},
                "color": "gray",
            },
        }

    build_call = json.dumps({
        "tool": "build",
        "arguments": {
            "description": "small shop near the SpawnLocation",
            "reference_path": "Workspace.SpawnLocation",
        },
    })
    plan_call = json.dumps({
        "tool": "plan_build",
        "arguments": {
            "description": "small shop near the SpawnLocation",
            "reference_path": "Workspace.SpawnLocation",
            "steps": [
                {"tool": "inspect_instance", "arguments": {"path": "Workspace.SpawnLocation"}},
                {"tool": "create_part", "arguments": {
                    "name": "ShopFloor",
                    "position": {"x": 5, "y": 0.5, "z": 0},
                    "size": {"x": 8, "y": 1, "z": 6},
                    "color": "gray",
                }},
                {"tool": "create_part", "arguments": {
                    "name": "ShopWall",
                    "position": {"x": 5, "y": 3, "z": -3},
                    "size": {"x": 8, "y": 5, "z": 1},
                    "color": "gray",
                }},
            ],
        },
    })
    inspect_spawn = json.dumps({
        "tool": "inspect_instance",
        "arguments": {"path": "Workspace.SpawnLocation"},
    })
    create_floor = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopFloor",
            "position": {"x": 5, "y": 0.5, "z": 0},
            "size": {"x": 8, "y": 1, "z": 6},
            "color": "gray",
        },
    })
    create_wall = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopWall",
            "position": {"x": 5, "y": 3, "z": -3},
            "size": {"x": 8, "y": 5, "z": 1},
            "color": "gray",
        },
    })
    final_report = json.dumps({
        "message": "Built a small shop near the SpawnLocation.",
    })

    verify_ok = {
        "Workspace/ShopFloor": {
            "ok": True,
            "result": {
                "name": "ShopFloor",
                "className": "Part",
                "path": "Workspace/ShopFloor",
            },
        },
        "Workspace/ShopWall": {
            "ok": True,
            "result": {
                "name": "ShopWall",
                "className": "Part",
                "path": "Workspace/ShopWall",
            },
        },
    }

    # 1) Successful plan: build, plan_build, execute planned steps, final report.
    provider = SequenceProvider([
        build_call,
        plan_call,
        inspect_spawn,
        create_floor,
        create_wall,
        final_report,
    ])
    rbx = BuildFakeRBX(
        {"inspect_instance": spawn_payload},
        verify_map=verify_ok,
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), part_payload("ShopWall")],
        },
    )
    result = make_agent(provider, rbx=rbx).run(
        "build a small shop near the SpawnLocation"
    )
    assert result.ok is True, result
    assert result.message == "Built a small shop near the SpawnLocation.", result
    assert [step["tool"] for step in result.steps] == [
        "build", "plan_build", "inspect_instance", "create_part", "create_part",
        "inspect_instance", "inspect_instance",
    ], [step["tool"] for step in result.steps]
    assert all(step["ok"] for step in result.steps), result.steps
    # Scene inspection happened once before execution; verification twice after.
    assert rbx.requests[0] == ("inspect_instance", {"path": "Workspace.SpawnLocation"})
    assert rbx.requests[1] == ("create_part", {
        "name": "ShopFloor",
        "position": {"x": 5, "y": 0.5, "z": 0},
        "size": {"x": 8, "y": 1, "z": 6},
        "color": "gray",
    })
    assert rbx.requests[2] == ("create_part", {
        "name": "ShopWall",
        "position": {"x": 5, "y": 3, "z": -3},
        "size": {"x": 8, "y": 5, "z": 1},
        "color": "gray",
    })
    print("OK  intelligent build plan: structured, bounded, executed, verified")

    # 2) plan_build outside build mode is rejected immediately.
    provider_outside = SequenceProvider([plan_call])
    rbx_outside = BuildFakeRBX({})
    result_outside = make_agent(provider_outside, rbx=rbx_outside).run(
        "plan a shop without entering build mode"
    )
    assert result_outside.ok is False, result_outside
    assert result_outside.error["code"] == "invalid_plan", result_outside
    assert [step["tool"] for step in result_outside.steps] == ["plan_build"], result_outside.steps
    print("OK  plan_build outside build mode rejected as invalid_plan")

    # 3) plan_build with a disallowed tool is rejected.
    bad_plan = json.dumps({
        "tool": "plan_build",
        "arguments": {
            "description": "bad plan",
            "steps": [
                {"tool": "asset_search", "arguments": {"query": "tree"}},
            ],
        },
    })
    provider_bad = SequenceProvider([build_call, bad_plan, final_report])
    rbx_bad = BuildFakeRBX({})
    result_bad = make_agent(provider_bad, rbx=rbx_bad).run("bad plan")
    assert result_bad.ok is False, result_bad
    assert result_bad.error["code"] == "build_failed", result_bad
    plan_step = result_bad.steps[1]
    assert plan_step["tool"] == "plan_build" and plan_step["ok"] is False, plan_step
    print("OK  plan_build with disallowed tool rejected")

    # 4) plan_build with duplicate consecutive steps is rejected.
    duplicate_plan = json.dumps({
        "tool": "plan_build",
        "arguments": {
            "description": "duplicate plan",
            "steps": [
                {"tool": "inspect_instance", "arguments": {"path": "Workspace.SpawnLocation"}},
                {"tool": "inspect_instance", "arguments": {"path": "Workspace.SpawnLocation"}},
            ],
        },
    })
    provider_dup = SequenceProvider([build_call, duplicate_plan, final_report])
    rbx_dup = BuildFakeRBX({})
    result_dup = make_agent(provider_dup, rbx=rbx_dup).run("duplicate plan")
    assert result_dup.ok is False, result_dup
    assert result_dup.error["code"] == "build_failed", result_dup
    print("OK  plan_build with duplicate consecutive steps rejected")

    # 5) plan_build with too many steps is rejected.
    oversized_plan = json.dumps({
        "tool": "plan_build",
        "arguments": {
            "description": "oversized plan",
            "steps": [
                {"tool": "create_part", "arguments": {"name": "P{0}".format(i), "position": {"x": i, "y": 0, "z": 0}, "size": {"x": 1, "y": 1, "z": 1}}}
                for i in range(6)
            ],
        },
    })
    provider_big = SequenceProvider([build_call, oversized_plan, final_report])
    rbx_big = BuildFakeRBX({})
    result_big = make_agent(provider_big, rbx=rbx_big).run("oversized plan")
    assert result_big.ok is False, result_big
    assert result_big.error["code"] == "build_failed", result_big
    print("OK  plan_build with more than 5 steps rejected")

    # 6) Redundant inspect_instance is skipped without a second plugin request.
    provider_skip = SequenceProvider([
        build_call,
        plan_call,
        inspect_spawn,
        inspect_spawn,
        create_floor,
        create_wall,
        final_report,
    ])
    rbx_skip = BuildFakeRBX(
        {"inspect_instance": spawn_payload},
        verify_map=verify_ok,
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), part_payload("ShopWall")],
        },
    )
    result_skip = make_agent(provider_skip, rbx=rbx_skip).run(
        "build a small shop near the SpawnLocation"
    )
    assert result_skip.ok is True, result_skip
    # The second inspect_instance step was skipped from cache but still recorded.
    inspect_steps = [s for s in result_skip.steps if s["tool"] == "inspect_instance"]
    assert len(inspect_steps) == 4, inspect_steps  # 2 model-driven (1 skipped) + 2 verify
    assert all(s["ok"] for s in inspect_steps), inspect_steps
    # Only one actual plugin request for the SpawnLocation inspection.
    spawn_requests = [r for r in rbx_skip.requests if r == ("inspect_instance", {"path": "Workspace.SpawnLocation"})]
    assert len(spawn_requests) == 1, spawn_requests
    print("OK  redundant inspect_instance skipped in build mode")

    # 7) Fallback summary when the model's final report is empty.
    provider_summary = SequenceProvider([
        build_call,
        plan_call,
        inspect_spawn,
        create_floor,
        create_wall,
        json.dumps({"message": ""}),
    ])
    rbx_summary = BuildFakeRBX(
        {"inspect_instance": spawn_payload},
        verify_map=verify_ok,
        per_tool_counts={
            "create_part": [part_payload("ShopFloor"), part_payload("ShopWall")],
        },
    )
    result_summary = make_agent(provider_summary, rbx=rbx_summary).run(
        "build a small shop near the SpawnLocation"
    )
    assert result_summary.ok is True, result_summary
    assert "ShopFloor" in result_summary.message and "ShopWall" in result_summary.message, result_summary.message
    print("OK  empty final report falls back to generated build summary")


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
    assert isinstance(tools, list) and len(tools) == 16, tools
    names = [tool["name"] for tool in tools]
    assert names == ["analyze_scene", "asset_search", "build", "create_part",
                     "create_script", "decompose_intent", "delete_instance",
                     "edit_build", "find_instances", "insert_asset",
                     "inspect_hierarchy", "inspect_instance", "modify_instance",
                     "plan_build", "recent_build_context", "recommend_assets"], names
    # The definitions are the model-facing JSON Schema (vec3 flattened), exactly
    # what Groq's `tools` parameter accepts.
    create_part = [tool for tool in tools if tool["name"] == "create_part"][0]
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


def scenario_iterative_build_editing():
    """Phase 8D: follow-up requests can edit an existing build. The Agent
    remembers a lightweight context of recently-created objects, reads it on
    edit_build, applies the smallest change (modify/create/delete), restricts
    deletion to recent build objects, and verifies every affected path."""
    mod = load_agent_module()
    registry = mod.rbxforge.default_registry()
    names = [t.name for t in registry.list()]
    assert "edit_build" in names, names
    assert "recent_build_context" in names, names
    assert "delete_instance" in names, names

    def part_payload(name, path=None, **overrides):
        result = {
            "name": name,
            "parent_path": "Workspace",
            "path": path or "Workspace/" + name,
            "position": {"x": 5, "y": 0.5, "z": 0},
            "size": {"x": 8, "y": 1, "z": 6},
            "color": "gray",
        }
        result.update(overrides)
        return {"ok": True, "result": result}

    def modify_payload(path, changed):
        return {
            "ok": True,
            "result": {"path": path, "className": "Part", "changed": changed},
        }

    def delete_payload(path, name):
        return {
            "ok": True,
            "result": {"path": path, "name": name, "className": "Part"},
        }

    def inspect_ok(name, path, **props):
        return {
            "ok": True,
            "result": {
                "name": name,
                "className": "Part",
                "path": path,
                "parent_path": "Workspace",
                "properties": props,
            },
        }

    def not_found(path):
        return {"ok": False, "error": {"code": "not_found", "message": "gone"}}

    build_call = json.dumps({
        "tool": "build",
        "arguments": {"description": "small shop"},
    })
    create_floor = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopFloor",
            "position": {"x": 5, "y": 0.5, "z": 0},
            "size": {"x": 8, "y": 1, "z": 6},
            "color": "gray",
        },
    })
    create_wall = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopWall",
            "position": {"x": 5, "y": 3, "z": -3},
            "size": {"x": 8, "y": 5, "z": 1},
            "color": "gray",
        },
    })
    create_sign = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "ShopSign",
            "position": {"x": 7, "y": 4, "z": 0},
            "size": {"x": 2, "y": 1, "z": 0.2},
            "color": "gray",
        },
    })
    final_build = json.dumps({
        "message": "Built a small shop with floor, wall, and sign.",
    })

    # 1) Build a shop and verify context is persisted on the Agent.
    # 2) Then add two windows to the existing build (reuse the same Agent).
    edit_add = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "add two windows"},
    })
    get_context = json.dumps({
        "tool": "recent_build_context",
        "arguments": {},
    })
    create_window1 = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "Window1",
            "position": {"x": 3, "y": 2, "z": -2.4},
            "size": {"x": 1.5, "y": 1.5, "z": 0.2},
            "color": "blue",
        },
    })
    create_window2 = json.dumps({
        "tool": "create_part",
        "arguments": {
            "name": "Window2",
            "position": {"x": 7, "y": 2, "z": -2.4},
            "size": {"x": 1.5, "y": 1.5, "z": 0.2},
            "color": "blue",
        },
    })
    final_add = json.dumps({"message": "Added two windows."})

    verify_map_build = {
        "Workspace/ShopFloor": inspect_ok("ShopFloor", "Workspace/ShopFloor"),
        "Workspace/ShopWall": inspect_ok("ShopWall", "Workspace/ShopWall"),
        "Workspace/ShopSign": inspect_ok("ShopSign", "Workspace/ShopSign"),
        "Workspace/Window1": inspect_ok("Window1", "Workspace/Window1"),
        "Workspace/Window2": inspect_ok("Window2", "Workspace/Window2"),
    }
    rbx_build = BuildFakeRBX(
        {},
        per_tool_counts={
            "create_part": [
                part_payload("ShopFloor"),
                part_payload("ShopWall"),
                part_payload("ShopSign", position={"x": 7, "y": 4, "z": 0},
                           size={"x": 2, "y": 1, "z": 0.2}),
                part_payload("Window1", position={"x": 3, "y": 2, "z": -2.4},
                            size={"x": 1.5, "y": 1.5, "z": 0.2}, color="blue"),
                part_payload("Window2", position={"x": 7, "y": 2, "z": -2.4},
                            size={"x": 1.5, "y": 1.5, "z": 0.2}, color="blue"),
            ],
        },
        verify_map=verify_map_build,
    )
    provider = SequenceProvider([
        build_call, create_floor, create_wall, create_sign, final_build,
        edit_add, get_context, create_window1, create_window2, final_add,
    ])
    agent = make_agent(provider, rbx=rbx_build)
    result_build = agent.run("build a small shop")
    assert result_build.ok is True, result_build
    assert len(agent.recent_build_context) == 3, agent.recent_build_context
    assert {entry["name"] for entry in agent.recent_build_context} == {
        "ShopFloor", "ShopWall", "ShopSign"
    }, agent.recent_build_context
    print("OK  build context persisted after successful build")

    result_add = agent.run("add two windows")
    assert result_add.ok is True, result_add
    assert result_add.message == "Added two windows.", result_add
    assert [s["tool"] for s in result_add.steps] == [
        "edit_build", "recent_build_context", "create_part", "create_part",
        "inspect_instance", "inspect_instance",
    ], [s["tool"] for s in result_add.steps]
    assert all(s["ok"] for s in result_add.steps), result_add.steps
    print("OK  edit_build adds components to an existing build")

    # 3) Modify an existing object (make the shop bigger) with a fresh Agent
    #    seeded from context.
    seeded_context = [
        {"path": "Workspace/ShopFloor", "name": "ShopFloor", "class": "Part",
         "position": {"x": 5, "y": 0.5, "z": 0},
         "size": {"x": 8, "y": 1, "z": 6}, "color": "gray"},
    ]
    agent2 = make_agent(SequenceProvider([]), rbx=BuildFakeRBX({}))
    agent2.recent_build_context = seeded_context
    agent2.rbx.set_recent_build_context(seeded_context)

    edit_bigger = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "make the shop bigger"},
    })
    inspect_floor = json.dumps({
        "tool": "inspect_instance",
        "arguments": {"path": "Workspace/ShopFloor"},
    })
    modify_floor = json.dumps({
        "tool": "modify_instance",
        "arguments": {
            "path": "Workspace/ShopFloor",
            "properties": {"size": {"x": 12, "y": 1, "z": 10}},
        },
    })
    final_bigger = json.dumps({"message": "Made the shop bigger."})

    provider_bigger = SequenceProvider([
        edit_bigger, get_context, inspect_floor, modify_floor, final_bigger,
    ])
    rbx_bigger = BuildFakeRBX(
        {},
        per_tool_counts={
            "modify_instance": [modify_payload("Workspace/ShopFloor", ["size"])],
        },
        verify_map={
            "Workspace/ShopFloor": inspect_ok(
                "ShopFloor", "Workspace/ShopFloor",
                Size={"x": 12, "y": 1, "z": 10},
            ),
        },
    )
    result_bigger = make_agent(provider_bigger, rbx=rbx_bigger).run(
        "make the shop bigger"
    )
    assert result_bigger.ok is True, result_bigger
    assert any(s["tool"] == "modify_instance" for s in result_bigger.steps), result_bigger.steps
    print("OK  edit_build modifies an existing object (resize)")

    # 4) Relative movement: move the counter to the left.
    seeded_context = [
        {"path": "Workspace/Counter", "name": "Counter", "class": "Part",
         "position": {"x": 5, "y": 0.5, "z": 0}, "size": {"x": 2, "y": 1, "z": 1},
         "color": "gray"},
    ]
    edit_move = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "move the counter to the left"},
    })
    inspect_counter = json.dumps({
        "tool": "inspect_instance",
        "arguments": {"path": "Workspace/Counter"},
    })
    modify_counter = json.dumps({
        "tool": "modify_instance",
        "arguments": {
            "path": "Workspace/Counter",
            "properties": {"position": {"x": 0, "y": 0.5, "z": 0}},
        },
    })
    final_move = json.dumps({"message": "Moved the counter to the left."})

    provider_move = SequenceProvider([
        edit_move, get_context, inspect_counter, modify_counter, final_move,
    ])
    rbx_move = BuildFakeRBX(
        {},
        per_tool_counts={
            "modify_instance": [modify_payload("Workspace/Counter", ["position"])],
        },
        verify_map={
            "Workspace/Counter": inspect_ok(
                "Counter", "Workspace/Counter",
                Position={"x": 0, "y": 0.5, "z": 0},
            ),
        },
    )
    agent_move = make_agent(provider_move, rbx=rbx_move)
    agent_move.recent_build_context = seeded_context
    agent_move.rbx.set_recent_build_context(seeded_context)
    result_move = agent_move.run("move the counter to the left")
    assert result_move.ok is True, result_move
    assert any(s["tool"] == "modify_instance" for s in result_move.steps), result_move.steps
    print("OK  edit_build repositions an existing object")

    # 5) Property change: change the roof to red (avoids creating a duplicate).
    seeded_context = [
        {"path": "Workspace/Roof", "name": "Roof", "class": "Part",
         "position": {"x": 5, "y": 5, "z": 0}, "size": {"x": 8, "y": 0.5, "z": 6},
         "color": "gray"},
    ]
    edit_color = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "change the roof to red"},
    })
    inspect_roof = json.dumps({
        "tool": "inspect_instance",
        "arguments": {"path": "Workspace/Roof"},
    })
    modify_roof = json.dumps({
        "tool": "modify_instance",
        "arguments": {
            "path": "Workspace/Roof",
            "properties": {"color": "red"},
        },
    })
    final_color = json.dumps({"message": "Changed the roof to red."})

    provider_color = SequenceProvider([
        edit_color, get_context, inspect_roof, modify_roof, final_color,
    ])
    rbx_color = BuildFakeRBX(
        {},
        per_tool_counts={
            "modify_instance": [modify_payload("Workspace/Roof", ["color"])],
        },
        verify_map={
            "Workspace/Roof": inspect_ok("Roof", "Workspace/Roof"),
        },
    )
    agent_color = make_agent(provider_color, rbx=rbx_color)
    agent_color.recent_build_context = seeded_context
    agent_color.rbx.set_recent_build_context(seeded_context)
    result_color = agent_color.run("change the roof to red")
    assert result_color.ok is True, result_color
    assert not any(s["tool"] == "create_part" for s in result_color.steps), result_color.steps
    assert any(s["tool"] == "modify_instance" for s in result_color.steps), result_color.steps
    print("OK  edit_build prefers modify over duplicate creation")

    # 6) Explicit deletion: remove the sign.
    seeded_context = [
        {"path": "Workspace/ShopSign", "name": "ShopSign", "class": "Part"},
    ]
    edit_delete = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "remove the sign"},
    })
    delete_sign = json.dumps({
        "tool": "delete_instance",
        "arguments": {"path": "Workspace/ShopSign"},
    })
    final_delete = json.dumps({"message": "Removed the sign."})

    provider_delete = SequenceProvider([
        edit_delete, get_context, delete_sign, final_delete,
    ])
    rbx_delete = BuildFakeRBX(
        {},
        per_tool_counts={
            "delete_instance": [delete_payload("Workspace/ShopSign", "ShopSign")],
        },
        verify_map={
            "Workspace/ShopSign": not_found("Workspace/ShopSign"),
        },
    )
    agent_delete = make_agent(provider_delete, rbx=rbx_delete)
    agent_delete.recent_build_context = seeded_context
    agent_delete.rbx.set_recent_build_context(seeded_context)
    result_delete = agent_delete.run("remove the sign you just created")
    assert result_delete.ok is True, result_delete
    assert any(s["tool"] == "delete_instance" and s["ok"] for s in result_delete.steps), result_delete.steps
    print("OK  edit_build deletes an object from the recent build context")

    # 7) Deletion safety: deleting an object outside the recent build context is
    #    rejected.
    edit_unsafe = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "remove the baseplate"},
    })
    delete_baseplate = json.dumps({
        "tool": "delete_instance",
        "arguments": {"path": "Workspace/Baseplate"},
    })
    final_unsafe = json.dumps({"message": "Cannot remove Baseplate."})

    provider_unsafe = SequenceProvider([
        edit_unsafe, get_context, delete_baseplate, final_unsafe,
    ])
    rbx_unsafe = BuildFakeRBX({})
    agent_unsafe = make_agent(provider_unsafe, rbx=rbx_unsafe)
    agent_unsafe.recent_build_context = seeded_context
    agent_unsafe.rbx.set_recent_build_context(seeded_context)
    result_unsafe = agent_unsafe.run("remove the baseplate")
    assert result_unsafe.ok is False, result_unsafe
    assert result_unsafe.error["code"] == "build_failed", result_unsafe
    del_step = [s for s in result_unsafe.steps if s["tool"] == "delete_instance"]
    assert del_step and del_step[0]["ok"] is False, del_step
    print("OK  delete_instance rejected outside recent build context")

    # 8) Partial failure during edit is reported as build_failed.
    edit_fail = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "make the shop bigger"},
    })
    modify_fail = json.dumps({
        "tool": "modify_instance",
        "arguments": {
            "path": "Workspace/ShopFloor",
            "properties": {"size": {"x": 12, "y": 1, "z": 10}},
        },
    })
    final_fail = json.dumps({"message": "Could not resize the shop."})

    provider_fail = SequenceProvider([
        edit_fail, get_context, modify_fail, final_fail,
    ])
    rbx_fail = BuildFakeRBX(
        {},
        per_tool_counts={
            "modify_instance": [{"ok": False, "error": {"code": "execution_failed", "message": "locked"}}],
        },
    )
    agent_fail = make_agent(provider_fail, rbx=rbx_fail)
    agent_fail.recent_build_context = seeded_context
    agent_fail.rbx.set_recent_build_context(seeded_context)
    result_fail = agent_fail.run("make the shop bigger")
    assert result_fail.ok is False, result_fail
    assert result_fail.error["code"] == "build_failed", result_fail
    print("OK  partial failure during edit reports build_failed")

    # 9) Ambiguous reference handled gracefully (no crash, final report).
    edit_ambiguous = json.dumps({
        "tool": "edit_build",
        "arguments": {"description": "make it bigger"},
    })
    final_ambiguous = json.dumps({
        "message": "Please clarify which object you want to make bigger.",
    })

    provider_ambiguous = SequenceProvider([
        edit_ambiguous, get_context, final_ambiguous,
    ])
    rbx_ambiguous = BuildFakeRBX({})
    agent_ambiguous = make_agent(provider_ambiguous, rbx=rbx_ambiguous)
    agent_ambiguous.recent_build_context = seeded_context
    agent_ambiguous.rbx.set_recent_build_context(seeded_context)
    result_ambiguous = agent_ambiguous.run("make it bigger")
    assert result_ambiguous.ok is True, result_ambiguous
    assert "clarify" in result_ambiguous.message.lower(), result_ambiguous.message
    print("OK  ambiguous edit request handled gracefully")


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


def scenario_analyze_scene():
    """Phase 9A: analyze_scene produces a bounded, deterministic summary of the
    Workspace, identifying landmarks, models, groups, class counts, and
    relevant objects. It handles empty scenes, large scenes, nested models,
    grouped structures, duplicate names, missing metadata, and bounded output."""
    mod = load_agent_module()
    registry = mod.rbxforge.default_registry()
    names = [t.name for t in registry.list()]
    assert "analyze_scene" in names, names

    def hierarchy_payload(tree, truncated=False):
        return {
            "ok": True,
            "result": {
                "root": "Workspace",
                "depth": 3,
                "count": _count_nodes(tree),
                "truncated": truncated,
                "tree": tree,
            },
        }

    def _count_nodes(tree):
        total = 0
        def walk(node):
            nonlocal total
            total += 1
            for child in node.get("children") or []:
                walk(child)
        for root in tree:
            walk(root)
        return total

    def node(name, cls, children=None):
        out = {"name": name, "className": cls}
        if children:
            out["children"] = children
        return out

    # 1) Empty scene: only Workspace.
    rbx_empty = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload([node("Workspace", "Workspace")]),
    })
    result_empty = mod.rbxforge.analyze_scene_summary(rbx_empty, {}, 10)
    assert result_empty is not None, result_empty
    summary_empty = result_empty["result"]
    assert summary_empty["total_nodes"] == 1, summary_empty
    assert summary_empty["landmarks"] == [], summary_empty
    assert summary_empty["models"] == [], summary_empty
    assert summary_empty["groups"] == [], summary_empty
    print("OK  analyze_scene handles empty scene")

    # 2) Landmarks, models, and groups.
    tree = [node("Workspace", "Workspace", [
        node("SpawnLocation", "SpawnLocation"),
        node("Baseplate", "Part"),
        node("Shop", "Model", [
            node("Shop_Floor", "Part"),
            node("Shop_Wall", "Part"),
            node("Shop_Roof", "Part"),
            node("Sign", "Part"),
        ]),
        node("Tree", "Model"),
        node("Script", "Script"),
    ])]
    rbx_scene = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(tree),
    })
    result_scene = mod.rbxforge.analyze_scene_summary(rbx_scene, {}, 10)
    summary = result_scene["result"]
    assert summary["total_nodes"] == _count_nodes(tree), summary
    assert any(l["name"] == "SpawnLocation" for l in summary["landmarks"]), summary
    assert any(l["name"] == "Baseplate" for l in summary["landmarks"]), summary
    assert any(m["name"] == "Shop" and m["child_count"] == 4 for m in summary["models"]), summary
    assert any(g["name"] == "Shop" and g["count"] == 3 for g in summary["groups"]), summary
    assert summary["class_counts"].get("Part", 0) >= 5, summary
    assert summary["class_counts"].get("Model", 0) == 2, summary
    print("OK  analyze_scene identifies landmarks, models, groups, and class counts")

    # 3) Relevant-object filtering via query uses find_instances.
    rbx_query = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(tree),
        "find_instances": {
            "ok": True,
            "result": {
                "query": "Tree",
                "max_results": 10,
                "total": 1,
                "count": 1,
                "truncated": False,
                "matches": [
                    {"name": "Tree", "className": "Model", "path": "Workspace/Tree"},
                ],
            },
        },
    })
    result_query = mod.rbxforge.analyze_scene_summary(
        rbx_query, {"query": "Tree"}, 10
    )
    summary_query = result_query["result"]
    assert any(r["name"] == "Tree" for r in summary_query["relevant"]), summary_query
    print("OK  analyze_scene includes query-relevant objects")

    # 4) Large scene truncated by max_nodes.
    big_tree = [node("Workspace", "Workspace", [
        node("Part{0}".format(i), "Part") for i in range(50)
    ])]
    rbx_big = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(big_tree),
    })
    result_big = mod.rbxforge.analyze_scene_summary(
        rbx_big, {"max_nodes": 10}, 10
    )
    summary_big = result_big["result"]
    assert summary_big["total_nodes"] == 10, summary_big
    assert summary_big["truncated"] is True, summary_big
    print("OK  analyze_scene respects max_nodes and reports truncation")

    # 5) Nested models.
    nested_tree = [node("Workspace", "Workspace", [
        node("House", "Model", [
            node("Frame", "Model", [
                node("Wall", "Part"),
            ]),
        ]),
    ])]
    rbx_nested = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(nested_tree),
    })
    result_nested = mod.rbxforge.analyze_scene_summary(rbx_nested, {}, 10)
    summary_nested = result_nested["result"]
    model_names = {m["name"] for m in summary_nested["models"]}
    assert model_names == {"House", "Frame"}, summary_nested
    print("OK  analyze_scene recognizes nested models")

    # 6) Duplicate names under different parents are counted.
    dup_tree = [node("Workspace", "Workspace", [
        node("Shop", "Model", [node("Wall", "Part")]),
        node("Garage", "Model", [node("Wall", "Part")]),
    ])]
    rbx_dup = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(dup_tree),
    })
    result_dup = mod.rbxforge.analyze_scene_summary(rbx_dup, {}, 10)
    summary_dup = result_dup["result"]
    assert summary_dup["class_counts"].get("Part", 0) == 2, summary_dup
    print("OK  analyze_scene handles duplicate names across parents")

    # 7) Missing metadata tolerated (className absent -> Unknown).
    bad_tree = [{"name": "Mystery"}]
    rbx_bad = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(bad_tree),
    })
    result_bad = mod.rbxforge.analyze_scene_summary(rbx_bad, {}, 10)
    summary_bad = result_bad["result"]
    assert summary_bad["class_counts"].get("Unknown", 0) == 1, summary_bad
    print("OK  analyze_scene tolerates missing className")

    # 8) Agent integration: analyze_scene used before a build.
    analyze_call = json.dumps({
        "tool": "analyze_scene",
        "arguments": {"query": "SpawnLocation"},
    })
    build_call = json.dumps({
        "tool": "build",
        "arguments": {"description": "small shop near SpawnLocation"},
    })
    final_build = json.dumps({
        "message": "Analyzed the scene and planned a shop near SpawnLocation.",
    })
    provider = SequenceProvider([analyze_call, build_call, final_build])
    rbx_agent = MultiFakeRBX({
        "inspect_hierarchy": hierarchy_payload(tree),
        "find_instances": {
            "ok": True,
            "result": {
                "query": "SpawnLocation",
                "max_results": 10,
                "total": 1,
                "count": 1,
                "truncated": False,
                "matches": [
                    {"name": "SpawnLocation", "className": "SpawnLocation", "path": "Workspace/SpawnLocation"},
                ],
            },
        },
    })
    result_agent = make_agent(provider, rbx=rbx_agent).run(
        "build a shop near the SpawnLocation"
    )
    assert result_agent.ok is True, result_agent
    assert [s["tool"] for s in result_agent.steps] == [
        "analyze_scene", "build",
    ], [s["tool"] for s in result_agent.steps]
    assert all(s["ok"] for s in result_agent.steps), result_agent.steps
    print("OK  analyze_scene integrates into the Agent loop before build")


def scenario_natural_language_build_intent():
    """Phase 9B: a vague natural-language build request is decomposed into a
    structured, bounded plan, then validated with plan_build and executed in
    build mode. The plan only contains actions the current toolset supports."""
    mod = load_agent_module()

    # Scene summary returned by analyze_scene (synthetic, but shaped like the
    # real tool output).
    scene_summary = {
        "total_nodes": 5,
        "truncated": False,
        "landmarks": [
            {"name": "SpawnLocation", "class": "SpawnLocation",
             "path": "Workspace/SpawnLocation"},
        ],
        "models": [
            {"name": "House", "class": "Model", "path": "Workspace/House",
             "child_count": 4},
        ],
        "groups": [],
        "class_counts": {"SpawnLocation": 1, "Model": 1, "Part": 1},
        "relevant": [],
    }

    # Decompose the request using the same module the tool uses.
    plan = intent.decompose_intent("build a small modern shop here", scene_summary)
    result = plan["result"]
    assert result["action"] == "build", result
    actions = result["required_actions"]
    assert 1 <= len(actions) <= 5, actions

    # Steps ready for plan_build.
    plan_steps = [{"tool": a["tool"], "arguments": a["arguments"]} for a in actions]

    # Provider sequence: analyze scene, decompose intent, enter build mode,
    # submit plan, execute each planned step, then report success.
    sequence = [
        json.dumps({"tool": "analyze_scene", "arguments": {}}),
        json.dumps({
            "tool": "decompose_intent",
            "arguments": {
                "request": "build a small modern shop here",
                "scene_summary": scene_summary,
            },
        }),
        json.dumps({
            "tool": "build",
            "arguments": {"description": "small modern shop near SpawnLocation"},
        }),
        json.dumps({
            "tool": "plan_build",
            "arguments": {
                "description": "small modern shop near SpawnLocation",
                "steps": plan_steps,
            },
        }),
    ]
    for a in actions:
        sequence.append(json.dumps({
            "tool": a["tool"],
            "arguments": a["arguments"],
        }))
    sequence.append(json.dumps({
        "message": "Built a small modern shop near the SpawnLocation.",
    }))

    # Canned create_part responses, one per planned step, each with a path so
    # build-mode verification can confirm the instance exists.
    create_payloads = []
    verify_map = {}
    for a in actions:
        name = a["arguments"]["name"]
        path = "Workspace/" + name
        create_payloads.append({
            "ok": True,
            "result": {
                "name": name,
                "path": path,
                "parent_path": "Workspace",
                "position": a["arguments"]["position"],
                "size": a["arguments"]["size"],
                "color": a["arguments"].get("color", "gray"),
            },
        })
        verify_map[path] = {
            "ok": True,
            "result": {
                "name": name,
                "className": "Part",
                "path": path,
                "parent_path": "Workspace",
                "properties": {},
            },
        }

    # Hierarchy tree used by analyze_scene.
    tree = [
        {"name": "Workspace", "className": "Workspace", "children": [
            {"name": "SpawnLocation", "className": "SpawnLocation"},
            {"name": "Baseplate", "className": "Part"},
            {"name": "House", "className": "Model", "children": [
                {"name": "Wall", "className": "Part"},
            ]},
        ]},
    ]

    def count_nodes(nodes):
        total = 0
        def walk(node):
            nonlocal total
            total += 1
            for child in node.get("children", []):
                walk(child)
        for node in nodes:
            walk(node)
        return total

    hierarchy_payload = {
        "ok": True,
        "result": {
            "root": "Workspace",
            "depth": 3,
            "count": count_nodes(tree),
            "truncated": False,
            "tree": tree,
        },
    }

    rbx = BuildFakeRBX(
        payloads={
            "inspect_hierarchy": hierarchy_payload,
            "find_instances": {
                "ok": True,
                "result": {
                    "query": "SpawnLocation",
                    "max_results": 10,
                    "total": 1,
                    "count": 1,
                    "truncated": False,
                    "matches": [
                        {"name": "SpawnLocation", "className": "SpawnLocation",
                         "path": "Workspace/SpawnLocation"},
                    ],
                },
            },
        },
        per_tool_counts={"create_part": create_payloads},
        verify_map=verify_map,
    )

    result = make_agent(SequenceProvider(sequence), rbx=rbx).run(
        "build a small modern shop here"
    )
    assert result.ok is True, result
    step_tools = [s["tool"] for s in result.steps]
    assert "analyze_scene" in step_tools, step_tools
    assert "decompose_intent" in step_tools, step_tools
    assert "build" in step_tools, step_tools
    assert "plan_build" in step_tools, step_tools
    assert sum(1 for s in result.steps if s["tool"] == "create_part") == len(actions), step_tools
    assert all(s["ok"] for s in result.steps), result.steps
    print("OK  natural-language build intent decomposes and executes through Agent")


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
    scenario_create_script_action_tool()
    scenario_insert_asset_action_tool()
    scenario_scene_aware_building()
    scenario_intelligent_build_planning()
    scenario_iterative_build_editing()
    scenario_analyze_scene()
    scenario_natural_language_build_intent()
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