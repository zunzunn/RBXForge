#!/usr/bin/env python3
"""RBXForge agent - Phase 4D: a bounded multi-step loop with project inspection.

Connects the Phase 3A provider layer (cli/providers.py) to the Phase 2B tool
layer (cli/rbxforge.py). A natural-language prompt is sent to a provider
together with the currently registered tool definitions. The model drives a
short, bounded loop:

    prompt -> model -> tool call -> ToolRegistry -> Studio -> result -> model -> ...

The model may call the inspection tools (find_instances, inspect_instance,
inspect_hierarchy) to gather live project context; each successful inspection
result is returned to the model as a bounded message, so it can decide the next
step. It eventually executes an action tool (create_part, create_script,
modify_instance), at which point the loop stops (modify_instance may be
followed by exactly one optional inspect_instance verification step) and a
concise final AgentResult is returned.

The read-only asset_search tool (Phase 7A) is intentionally NOT a Studio /
plugin tool: it searches the public Creator Store over the official Open Cloud
API with a local HTTP request, so it is exposed to the model alongside the
plugin tools but never ends the loop. recommend_assets (Phase 7B) ranks the
search results into a bounded, deterministic, explainable recommendation list;
it is likewise read-only, local, and never ends the loop.

Each step's reply is one JSON object - either a tool call:

    {"tool": "<tool name>", "arguments": { ... }}

or a final report (when the model decides no tool call is needed):

    {"message": "<what it did or decided>"}

Every tool call goes through the existing ToolRegistry (validation is
unchanged); unknown tools and invalid arguments are rejected safely instead of
being executed. The loop is bounded: at most MAX_TOOL_CALLS executed tool calls
per request, and only bounded, compacted tool results are ever shown to the
model (never unbounded hierarchy/property data). Single-step requests keep the
previous behavior: one model call -> one tool call -> done.

Explicitly out of scope: no new Studio tools, no plugin/protocol changes, no
arbitrary Lua/code execution, no automatic modification except through the
registered action tools. The Studio plugin and WebSocket protocol are untouched.

Standard library only; no external dependencies.

Usage (one-shot, provider from the environment - see cli/providers.py):
    python3 cli/agent.py "create a red cube"
"""

import json
import os
import sys

# The cli/ directory is not a package, so this module locates its sibling
# modules (providers.py, rbxforge.py) directly. When run as a script the
# interpreter already places this directory first on sys.path, so this is
# effectively a no-op; it matters for in-process test loading.
_HERE = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import providers  # Phase 3A provider layer (Provider, ProviderError, ...) - noqa: E402
import rbxforge  # Phase 2B tool layer (Tool, ToolRegistry, ...) - noqa: E402

# --------------------------------------------------------------------------- #
# Structured tool call parsing
# --------------------------------------------------------------------------- #


class ToolCall:
    """A parsed, structured tool call: a tool name plus its arguments."""

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments

    def __repr__(self):
        return "ToolCall(name={0!r}, arguments={1!r})".format(self.name, self.arguments)


class FinalMessage:
    """A model reply that finishes the task without another tool call:
    ``{"message": "..."}``."""

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "FinalMessage(text={0!r})".format(self.text)


class ToolCallParseError(Exception):
    """Raised when provider output cannot be parsed into a tool call."""


def _first_json_object(text):
    """Return the first balanced JSON object in ``text``, or None.

    Scans character by character, tracking string escapes and brace depth, so a
    nested object/array inside string values does not confuse the boundaries.
    """
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : index + 1]
    return None


def parse_tool_call(text):
    """Parse a structured tool call from provider output.

    The model is instructed to reply with exactly one JSON object
    (``{"tool": ..., "arguments": {...}}``). This parser accepts that object
    anywhere in the text - including inside a ```json fenced block - using the
    first JSON object found. Returns a :class:`ToolCall` or raises
    :class:`ToolCallParseError` for any malformed output.
    """
    candidates = []
    if not isinstance(text, str) or not text.strip():
        raise ToolCallParseError("model produced no output")
    candidates.append(_first_json_object(text))
    if candidates[0] is None:
        raise ToolCallParseError(
            "model output contains no JSON object: {0!r}".format(text[:200])
        )
    try:
        data = json.loads(candidates[0])
    except ValueError as exc:
        raise ToolCallParseError("model output is not valid JSON: {0}".format(exc))
    if not isinstance(data, dict):
        raise ToolCallParseError("structured tool call must be a JSON object")
    if not isinstance(data.get("tool"), str) or not data["tool"].strip():
        raise ToolCallParseError("structured tool call is missing a 'tool' name")
    arguments = data.get("arguments")
    if not isinstance(arguments, dict):
        raise ToolCallParseError("structured tool call 'arguments' must be an object")
    return ToolCall(data["tool"].strip(), arguments)


def parse_agent_reply(text):
    """Parse one model reply from the multi-step loop.

    Accepted shapes (the first JSON object in the text is used):

    - a tool call: ``{"tool": "...", "arguments": {...}}`` -> :class:`ToolCall`
    - a final report: ``{"message": "..."}`` -> :class:`FinalMessage`

    Raises :class:`ToolCallParseError` for any malformed output (no JSON, a
    JSON array, a missing or bad ``tool``/``arguments``/``message``, ...).
    """
    if not isinstance(text, str) or not text.strip():
        raise ToolCallParseError("model produced no output")
    obj = _first_json_object(text)
    if obj is None:
        raise ToolCallParseError(
            "model output contains no JSON object: {0!r}".format(text[:200])
        )
    try:
        data = json.loads(obj)
    except ValueError as exc:
        raise ToolCallParseError("model output is not valid JSON: {0}".format(exc))
    if not isinstance(data, dict):
        raise ToolCallParseError("structured reply must be a JSON object")
    if "message" in data:
        if isinstance(data["message"], str):
            return FinalMessage(data["message"].strip())
        raise ToolCallParseError("a final 'message' must be a string")
    if not isinstance(data.get("tool"), str) or not data["tool"].strip():
        raise ToolCallParseError(
            "structured reply must be a tool call (with 'tool' and 'arguments') "
            "or a final report (with 'message')"
        )
    arguments = data.get("arguments")
    if not isinstance(arguments, dict):
        raise ToolCallParseError("structured tool call 'arguments' must be an object")
    return ToolCall(data["tool"].strip(), arguments)


# --------------------------------------------------------------------------- #
# Bounded tool results (Phase 4D)
# --------------------------------------------------------------------------- #

#: Tool names that change the project. Calling an action tool ends the loop
#: (Phase 6B: modify_instance allows exactly one optional inspect_instance
#: verification step before the loop ends; Phase 7C: insert_asset does the
#: same so the model can verify the placed asset).
ACTION_TOOLS = frozenset(
    {
        "create_part",
        "create_script",
        "modify_instance",
        "insert_asset",
        "delete_instance",
    }
)

#: Hard bound on executed tool calls per user request.
MAX_TOOL_CALLS = 5

#: Raised bound when the model explicitly enters build mode (Phase 8A). A
#: build needs room for scene inspection, the build declaration, several
#: create/insert actions, and a final verification pass, while still staying
#: bounded and deterministic.
BUILD_MODE_MAX_TOOL_CALLS = 12

#: Bounds applied to tool results before they are shown to the model.
MAX_TOOL_RESULT_ITEMS = 20  # cap on list/dict entries (e.g. matches, children)
MAX_TOOL_RESULT_STRING = 200  # per-string truncation length
MAX_TOOL_RESULT_CHARS = 2000  # serialized result budget


def _compact_value(value, depth=0):
    """Return a JSON-serializable, size-bounded copy of ``value``.

    Arrays and dict entry lists are capped at :data:`MAX_TOOL_RESULT_ITEMS`,
    strings at :data:`MAX_TOOL_RESULT_STRING`, and nesting at a fixed depth, so
    no unbounded hierarchy/property data can reach the model even if the plugin
    returned more than its own per-tool limits claim.
    """
    if depth > 10:
        return "..."
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_TOOL_RESULT_STRING:
            return value
        return value[:MAX_TOOL_RESULT_STRING] + "..."
    if isinstance(value, list):
        return [
            _compact_value(item, depth + 1) for item in value[:MAX_TOOL_RESULT_ITEMS]
        ]
    if isinstance(value, dict):
        return {
            key: _compact_value(item, depth + 1)
            for key, item in list(value.items())[:MAX_TOOL_RESULT_ITEMS]
        }
    return repr(value)[:MAX_TOOL_RESULT_STRING]


def compact_tool_result(call, output, response_payload):
    """Render the bounded tool-result text shown to the model for one call.

    ``response_payload`` is the plugin's ``response`` payload captured from the
    tool's ``send_request`` (or None when the tool produced no response). The
    returned text is always bounded by :data:`MAX_TOOL_RESULT_CHARS`.
    """
    if response_payload is not None:
        try:
            text = json.dumps(_compact_value(response_payload), sort_keys=True)
        except (TypeError, ValueError):
            text = repr(output)
    else:
        # A local call like asset_search (Phase 7A) produces no send_request
        # response payload, so compact the tool's own structured output the
        # same way instead of dumping a raw repr of the dict.
        try:
            text = json.dumps(_compact_value(output), sort_keys=True)
        except (TypeError, ValueError):
            text = repr(output)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        suffix = "...\n[result truncated for length]"
        keep = max(0, MAX_TOOL_RESULT_CHARS - len(suffix))
        text = text[:keep] + suffix
    return text


def tool_result_message(index, call, output, response_payload):
    """The message appended after a tool executes, so the next model turn can
    act on what actually happened in Studio."""
    return "Tool call #{0}: {1}\nArguments: {2}\nResult: {3}".format(
        index,
        call.name,
        json.dumps(call.arguments, sort_keys=True),
        compact_tool_result(call, output, response_payload),
    )


class CapturingRBX:
    """Wrap the connection handed to the ToolRegistry so tool responses can be
    captured for the model while the registry and the tool layer are untouched.

    Tools call ``send_request`` and ``log`` exactly as they do on the real
    connection; the wrapper delegates both and records each ``send_request``
    response. This is how the agent observes inspection results without changing
    any tool or validation behavior.
    """

    def __init__(self, rbx):
        self._rbx = rbx
        self.responses = []

    def send_request(self, tool, params, timeout):
        response = self._rbx.send_request(tool, params, timeout)
        self.responses.append({"tool": tool, "params": params, "response": response})
        return response

    def log(self, message):
        return self._rbx.log(message)

    def assets(self):
        resolver = getattr(self._rbx, "assets", None)
        if resolver is not None:
            return resolver()
        return None

    def remember_assets(self, results):
        """Delegate the Phase 7C known-assets recording to the wrapped
        connection so asset ids persist even though tools run through this
        wrapper (insert_asset validates ids against them)."""
        remember = getattr(self._rbx, "remember_assets", None)
        if remember is not None:
            return remember(results)
        return None

    def asset_known(self, asset_id):
        lookup = getattr(self._rbx, "asset_known", None)
        if lookup is not None:
            return lookup(asset_id)
        return None

    def known_asset(self, asset_id):
        lookup = getattr(self._rbx, "known_asset", None)
        if lookup is not None:
            return lookup(asset_id)
        return None


# --------------------------------------------------------------------------- #
# Tool definitions for the model
# --------------------------------------------------------------------------- #


def _model_schema(schema):
    """Flatten an internal RBXForge schema into a model-friendly JSON Schema.

    The registry validator understands our custom ``"vec3"`` type, but that bare
    string is opaque to an LLM - a model that sees ``{"type": "vec3"}`` guesses
    the format (arrays like ``[0,5,0]``, strings like ``"0,5,0"``), which the
    validator rightly rejects. Present vectors as a plain object schema with
    numeric ``x``/``y``/``z`` properties and a description showing the exact
    form, so the model reliably produces ``{"x": ..., "y": ..., "z": ...}``.
    Validation itself is untouched.
    """
    kind = schema.get("type")
    if kind == "vec3":
        return {
            "type": "object",
            "description": "a 3D vector as an object with numeric x, y, z, "
            'e.g. {"x": 0, "y": 5, "z": 0} - never an array or '
            'a string like "0,5,0"',
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        }
    if kind == "object":
        out = dict(schema)
        out["properties"] = {
            key: _model_schema(child)
            for key, child in schema.get("properties", {}).items()
        }
        return out
    return dict(schema)


def tool_definitions(registry):
    """Return the registered tools as a serializable list for the model.

    Each entry carries the same metadata the CLI validates against - name,
    description, and parameters - so the model can select a tool and supply
    schema-valid arguments. Parameters are flattened via :func:`_model_schema`
    so internal shorthand like ``vec3`` is described in a format the model
    understands while the validator keeps its strict checks.
    """
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": _model_schema(tool.input_schema),
        }
        for tool in registry.list()
    ]


def build_system_prompt(registry):
    """Build the system message describing available tools, the multi-step
    loop, and the reply format."""
    tools_json = json.dumps(tool_definitions(registry))
    return (
        "You are the RBXForge building agent. You act in short steps, calling "
        "RBXForge tools to inspect the project before deciding, then to make "
        "changes.\n"
        "Available tools:\n" + tools_json + "\n"
        "Reply with exactly one JSON object per step, either:\n"
        '  - a tool call: {"tool": "<tool name>", "arguments": { ... }}\n'
        '  - a final report: {"message": "<what you did or decided>"} - only '
        "when you are finished\n"
        "Rules:\n"
        "- Use the inspection tools (find_instances, inspect_instance, "
        "inspect_hierarchy) to gather live project context first; their results "
        "are returned to you on the next step.\n"
        "- find_instances locates instances by name; inspect_instance reads the "
        "safe properties of one instance by full path.\n"
        "- asset_search reads the public Roblox Creator Store over the Open "
        "Cloud API (read-only, nothing is inserted or purchased); use it to "
        "find real assets when the prompt asks for them.\n"
        "- recommend_assets ranks asset_search results into a bounded, "
        "deterministic, explainable list (each recommendation comes with a "
        "score and a reason: title/description term matches, asset type, "
        "creator, rating/usage). Prefer it when the user asks for a "
        "recommendation ('what should I use', 'best ...'); it is also "
        "read-only and never downloads, inserts, or purchases anything.\n"
        "- insert_asset inserts the Creator Store asset whose 'asset_id' came "
        "exactly from a prior asset_search / recommend_assets result (ids are "
        "never invented - an id that was not returned by a search is "
        "rejected). It validates the asset, then the plugin loads it and "
        "places it: give a vec3 'position', or a 'reference_path' to place it "
        "near an existing instance (e.g. when the user says 'near the "
        "SpawnLocation', inspect the scene first for its path), or neither to "
        "use the plugin's default spot. After insert_asset succeeds the system "
        "automatically verifies the placed instance at its reported path, so "
        "do not call inspect_instance yourself; report as soon as insert_asset "
        "succeeds.\n"
        "- build declares a scene-aware multi-object build plan (Phase 8A). "
        "Use it when the user asks for a structure that requires several "
        "objects (e.g. 'build a small shop near the SpawnLocation' or 'make a "
        "garage next to the house'). Call build first with a clear description "
        "and optional reference_path, then submit a structured plan with "
        "plan_build (Phase 8B): list up to 5 explicit steps using only "
        "create_part, create_script, insert_asset, modify_instance, or scene "
        "inspection tools. Inspect the scene if the position depends on "
        "existing objects, then execute the planned steps. Action tools do not "
        "end the loop in build mode. When you are done send a final report in "
        "plain language describing exactly what was built; the system will "
        "verify every created path and fail the build if any step failed or "
        "any path is missing. Do not use build for single-object requests.\n"
        "- analyze_scene reads the current Workspace and returns a compact summary "
        "of landmarks, major models, related object groups, class counts, and "
        "optionally objects matching a query (Phase 9A). Use it at the start of a "
        "complex build or edit request, or when the user refers to the existing "
        "scene (e.g. 'build a shop near the big tree' or 'make the house taller'), "
        "so you can understand the scene before acting instead of making many "
        "individual inspection calls. It is read-only and never changes the project.\n"
        "- decompose_intent turns a natural-language build/edit request into a "
        "structured, bounded execution plan (Phase 9B). After analyze_scene, call "
        "decompose_intent with the user's exact request and the scene_summary to "
        "get a plan that lists only actions the current toolset can perform, plus "
        "dependencies, spatial constraints, verification criteria, and any "
        "unsupported capabilities. Then use build/plan_build for new structures or "
        "edit_build for changes to existing objects; execute only the supported "
        "steps from the plan. It is read-only and never changes the project.\n"
        "- edit_build declares an iterative edit to an existing build (Phase 8D). "
        "Use this for follow-up requests like 'make the shop bigger', 'move the "
        "counter to the left', 'change the roof to red', 'add two windows', or "
        "'remove the sign you just created'. Call edit_build first with a clear "
        "description, then read recent_build_context to see the objects created "
        "in the last build. Inspect the scene if needed, then apply the smallest "
        "set of changes: use modify_instance to resize, recolor, or reposition "
        "an existing object; use create_part/create_script/insert_asset only to "
        "add genuinely new components; use delete_instance ONLY when the user "
        "explicitly asks to remove an object and the path is from recent_build_context. "
        "Never create a duplicate when an existing object can be modified instead. "
        "Action tools do not end the loop in edit mode. When you are done send a "
        "final report in plain language describing exactly what changed; the system "
        "will verify every modified, added, and removed path and fail the edit if "
        "any step failed.\n"
        "- create_part and create_script change the project; once a change tool reports "
        "success, the model may call inspect_instance exactly once to verify the "
        "result if the target can be resolved and verification is useful; "
        "verification is skipped when the tool result already provides sufficient "
        "information and would be redundant. The task is complete after verification "
        "or when the model decides not to verify.\n"
        "- modify_instance also changes the project: it modifies an allowlisted "
        "set of properties on one instance by path. After it succeeds you may "
        "call inspect_instance exactly once to verify the change if your "
        "reasoning requires it, then report; do not call any other tools "
        "afterwards.\n"
        "- If a request is already simple (e.g. 'create a red cube'), make the "
        "single tool call you need immediately instead of exploring.\n"
        "- If you conclude no tool call is needed, reply with a final report.\n"
        "- 'arguments' must satisfy the selected tool's parameters schema "
        "exactly.\n"
        "3D vectors (e.g. position, size) are JSON objects of the form "
        '{"x": number, "y": number, "z": number} - never arrays like [0,5,0] '
        'and never strings like "0,5,0".\n'
        "Example call:\n"
        '{"tool": "create_part", "arguments": {"name": "RedCube", '
        '"position": {"x": 0, "y": 5, "z": 0}, '
        '"size": {"x": 4, "y": 4, "z": 4}, "color": "red"}}\n'
        "Do not include any other text in your reply."
    )


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class AgentResult:
    """Outcome of a single :meth:`Agent.run` call (the concise final report).

    - ``ok``: True when the request either completed through an action tool or
      ended with a final model report.
    - ``tool``: the action :class:`ToolCall` that completed the task, or None
      when no action tool executed (a final-report completion or pure-inspection
      loop). For a successful ``modify_instance`` the action call is reported
      even when the loop also ran the single optional verification step (the
      verification is recorded in ``steps``). For ``insert_asset`` the loop
      automatically runs one ``inspect_instance`` verification step (Phase 7D)
      and reports failure if verification does not match.
    - ``output``: the last tool execution result from the tool layer (e.g. bool).
    - ``message``: a final model report, or None.
    - ``steps``: ordered record of each parsed tool call and its outcome as
      ``{"tool", "arguments", "output", "data", "result", "ok"}`` where ``data``
      is the bounded (compacted) plugin response, ``result`` is the bounded
      text that was shown to the model, and ``ok`` is False for calls that were
      rejected or failed before/during execution.
    - ``error``: None, or ``{"code": ..., "message": ...}`` (+ ``type`` for
      provider errors).
    - ``provider_text``: the raw last provider text, for diagnostics.
    """

    def __init__(
        self,
        ok,
        tool=None,
        output=None,
        error=None,
        provider_text=None,
        message=None,
        steps=None,
    ):
        self.ok = ok
        self.tool = tool
        self.output = output
        self.error = error
        self.provider_text = provider_text
        self.message = message
        self.steps = steps if steps is not None else []

    def __repr__(self):
        return "AgentResult(ok={0!r}, error={1!r}, tool={2!r})".format(
            self.ok, self.error, self.tool
        )


class Agent:
    """Bounded multi-step agent: prompt -> model -> tool call -> execution -> ...

    Phase 4D/8B; preserves the Phase 3B single-step behavior for simple requests
    and adds scene-aware multi-object build mode with structured planning. The
    loop is bounded per user request by ``max_tool_calls`` (default 5), and only
    compacted, bounded tool results are ever returned to the model.

    - ``provider``: a Phase 3A ``Provider`` instance (Ollama, mock, ...).
    - ``registry``: the :class:`ToolRegistry` tools are validated and executed
      through (defaults to the built-in registry).
    - ``rbx``: the object handed to the registry when executing a tool - an
      ``RBXForge`` connection in real use, a fake in tests.
    - ``timeout``: per-tool execution timeout in seconds.
    - ``max_tool_calls``: hard bound on tool calls per request.
    """

    def __init__(
        self,
        provider,
        registry=None,
        rbx=None,
        timeout=10.0,
        max_tool_calls=MAX_TOOL_CALLS,
    ):
        self.provider = provider
        self.registry = (
            registry if registry is not None else rbxforge.default_registry()
        )
        self.rbx = rbx if rbx is not None else rbxforge.RBXForge()
        self.timeout = timeout
        self.max_tool_calls = max_tool_calls
        # Phase 8D: lightweight context of objects created by the most recent
        # successful build. Persisted across run() calls so follow-up edit
        # requests can reference them naturally.
        self.recent_build_context = []

    def tool_definitions(self):
        """The currently registered tool definitions sent to the model."""
        return tool_definitions(self.registry)

    def _verify_inserted_asset(self, insert_response, timeout):
        """Phase 7D: after a successful insert_asset, verify the instance exists
        at the reported path and matches the reported name/class.

        Returns ``(verified: bool, info: dict)`` where ``info`` contains the
        inspect_instance arguments, output, compacted data, result text, and a
        human-readable message. On failure ``verified`` is False and
        ``info["message"]`` explains why.
        """
        info = {
            "arguments": None,
            "output": False,
            "data": None,
            "result": None,
            "message": None,
        }
        if not insert_response or not insert_response.get("ok"):
            info["message"] = "insert_asset did not report success"
            return False, info

        result = insert_response.get("result") or {}
        path = result.get("path")
        if not path:
            info["message"] = "insert_asset succeeded but did not return a path to verify"
            return False, info

        expected_name = result.get("name")
        expected_class = result.get("class")
        arguments = {"path": path}
        info["arguments"] = arguments

        capturer = CapturingRBX(self.rbx)
        try:
            output = self.registry.execute(
                capturer, "inspect_instance", arguments, timeout
            )
        except (rbxforge.UnknownToolError, rbxforge.InvalidParamsError) as exc:
            info["message"] = "verification tool error: " + str(exc)
            return False, info

        response_payload = (
            capturer.responses[-1]["response"] if capturer.responses else None
        )
        info["output"] = output
        info["data"] = (
            _compact_value(response_payload)
            if response_payload is not None
            else None
        )
        info["result"] = compact_tool_result(
            ToolCall(name="inspect_instance", arguments=arguments),
            output,
            response_payload,
        )

        if not output or not response_payload or not response_payload.get("ok"):
            info["message"] = (
                "insert_asset reported success but verification of {0} failed".format(
                    path
                )
            )
            return False, info

        inspected = response_payload.get("result") or {}
        actual_path = inspected.get("path")
        actual_name = inspected.get("name")
        actual_class = inspected.get("className") or inspected.get("class")

        if actual_path != path:
            info["message"] = "verification path mismatch: expected {0}, found {1}".format(
                path, actual_path
            )
            return False, info
        if expected_name is not None and actual_name != expected_name:
            info["message"] = (
                "verification name mismatch at {0}: expected {1}, found {2}".format(
                    path, expected_name, actual_name
                )
            )
            return False, info
        if expected_class is not None and actual_class != expected_class:
            info["message"] = (
                "verification class mismatch at {0}: expected {1}, found {2}".format(
                    path, expected_class, actual_class
                )
            )
            return False, info

        info["message"] = "Verified insertion: asset {0} is {1} ({2}) at {3}".format(
            result.get("asset_id"), actual_name, actual_class, path
        )
        return True, info

    @staticmethod
    def _extract_created_path(tool_name, response_payload):
        """Phase 8A: extract the reported instance path from a successful
        action-tool response so build-mode final verification can confirm the
        instance still exists. Returns None when the tool does not report a
        path or the response is missing."""
        if not response_payload or not response_payload.get("ok"):
            return None
        result = response_payload.get("result") or {}
        return result.get("path")

    def _verify_build_paths(self, paths, timeout):
        """Phase 8A: verify that every path created during a build still exists
        and matches the reported name/class.

        Returns ``(verified: bool, info: dict)`` where ``info`` contains
        ``arguments``, ``output``, ``data``, ``result``, ``message``, and a
        ``paths`` list of ``{path, ok, name?, class?}``. On failure
        ``info["message"]`` explains which path failed.
        """
        info = {
            "arguments": None,
            "output": True,
            "data": [],
            "result": [],
            "message": None,
            "paths": [],
        }
        if not paths:
            info["message"] = "no created paths to verify"
            return True, info

        for path in paths:
            capturer = CapturingRBX(self.rbx)
            try:
                output = self.registry.execute(
                    capturer, "inspect_instance", {"path": path}, timeout
                )
            except (rbxforge.UnknownToolError, rbxforge.InvalidParamsError) as exc:
                info["paths"].append({"path": path, "ok": False, "error": str(exc)})
                info["output"] = False
                continue

            response_payload = (
                capturer.responses[-1]["response"] if capturer.responses else None
            )
            ok = bool(output and response_payload and response_payload.get("ok"))
            entry = {"path": path, "ok": ok}
            if ok:
                result = response_payload.get("result") or {}
                entry["name"] = result.get("name")
                entry["class"] = result.get("className") or result.get("class")
            info["paths"].append(entry)
            if not ok:
                info["output"] = False

        if not info["output"]:
            failed = [p["path"] for p in info["paths"] if not p["ok"]]
            info["message"] = (
                "build verification failed: the following created paths could not "
                "be confirmed: {0}".format(", ".join(failed))
            )
            return False, info

        info["message"] = "build verified: {0} path(s) confirmed".format(len(paths))
        return True, info

    @staticmethod
    def _build_summary(build_plan, build_paths):
        """Phase 8B: produce a simple natural-language summary of a completed
        build from the recorded created paths. Falls back to the plan
        description when no paths were recorded."""
        description = "the build"
        if build_plan and build_plan.get("description"):
            description = build_plan["description"]
        if not build_paths:
            return "Planned {0} but no objects were created.".format(description)
        names = []
        for path in build_paths:
            # Use the last segment of the path as the object name.
            segment = path.split("/")[-1]
            if segment and segment not in names:
                names.append(segment)
        if not names:
            return "Build completed with {0} object(s).".format(len(build_paths))
        if len(names) == 1:
            return "Built {0}: {1}.".format(description, names[0])
        return "Built {0}: {1} and {2}.".format(
            description, ", ".join(names[:-1]), names[-1]
        )

    @staticmethod
    def _extract_build_context_entry(tool_name, response_payload):
        """Phase 8D: build a lightweight context entry from a creation response."""
        if not response_payload or not response_payload.get("ok"):
            return None
        result = response_payload.get("result") or {}
        path = result.get("path")
        if not path:
            return None
        entry = {
            "path": path,
            "name": result.get("name"),
            "class": result.get("className") or result.get("class"),
        }
        if tool_name in ("create_part", "modify_instance"):
            for key in ("position", "size", "color", "material"):
                if key in result:
                    entry[key] = result[key]
        elif tool_name == "insert_asset":
            for key in ("position", "placement"):
                if key in result:
                    entry[key] = result[key]
        elif tool_name == "create_script":
            entry["type"] = result.get("type")
            entry["parent_path"] = result.get("parent_path")
        return entry

    def _persist_build_context(self, paths, steps):
        """Phase 8D: populate ``self.recent_build_context`` from a completed
        build and mirror it to ``self.rbx`` so the registry tool can read it.
        """
        context = []
        seen = set()
        for step in steps:
            if step.get("tool") in ACTION_TOOLS and step.get("ok"):
                path = self._extract_created_path(step["tool"], step.get("data"))
                if path and path not in seen:
                    seen.add(path)
                    entry = self._extract_build_context_entry(
                        step["tool"], step.get("data")
                    )
                    if entry:
                        context.append(entry)
        # Ensure every verified path is represented even if the creation
        # response was sparse.
        for path in paths:
            if path not in seen:
                seen.add(path)
                context.append({"path": path, "name": path.split("/")[-1]})
        self.recent_build_context = context[:rbxforge.MAX_RECENT_BUILD_CONTEXT]
        setter = getattr(self.rbx, "set_recent_build_context", None)
        if setter is not None:
            setter(self.recent_build_context)

    def _verify_edit_paths(self, modified, created, deleted, timeout):
        """Phase 8D: verify an edit batch. Modified and created paths must
        exist; deleted paths must be gone."""
        info = {"output": True, "paths": []}

        def check(path, expect_exists):
            capturer = CapturingRBX(self.rbx)
            try:
                output = self.registry.execute(
                    capturer, "inspect_instance", {"path": path}, timeout
                )
            except (rbxforge.UnknownToolError, rbxforge.InvalidParamsError) as exc:
                return {"path": path, "ok": not expect_exists, "error": str(exc)}
            response_payload = (
                capturer.responses[-1]["response"] if capturer.responses else None
            )
            exists = bool(
                output and response_payload and response_payload.get("ok")
            )
            ok = exists if expect_exists else not exists
            return {"path": path, "ok": ok, "exists": exists}

        for path in modified + created:
            entry = check(path, True)
            info["paths"].append(entry)
            if not entry["ok"]:
                info["output"] = False
        for path in deleted:
            entry = check(path, False)
            info["paths"].append(entry)
            if not entry["ok"]:
                info["output"] = False

        if not info["output"]:
            failed = [p["path"] for p in info["paths"] if not p["ok"]]
            info["message"] = (
                "edit verification failed: the following paths were not in the "
                "expected state: {0}".format(", ".join(failed))
            )
            return False, info
        info["message"] = "edit verified: {0} path(s) confirmed".format(
            len(info["paths"])
        )
        return True, info

    @staticmethod
    def _edit_summary(edit_description, modified, created, deleted):
        """Phase 8D: produce a simple natural-language summary of an edit."""
        parts = []
        if modified:
            names = sorted({p.split("/")[-1] for p in modified})
            parts.append("updated " + ", ".join(names))
        if created:
            names = sorted({p.split("/")[-1] for p in created})
            parts.append("added " + ", ".join(names))
        if deleted:
            names = sorted({p.split("/")[-1] for p in deleted})
            parts.append("removed " + ", ".join(names))
        if not parts:
            return "No changes were made."
        action = "; ".join(parts)
        if edit_description:
            return "Edited {0}: {1}.".format(edit_description, action)
        return "Edited {0}.".format(action)

    def run(self, prompt, **chat_options):
        """Run ``prompt`` through the bounded multi-step loop.

        Returns a concise :class:`AgentResult`. Provider failures, malformed
        model output, unknown tools, invalid arguments, execution failures, and
        hitting the per-request tool-call budget are all returned as failures
        (never raised), so this is safe to call from the REPL or CLI.

        Single-step requests behave as before: a model that replies with a
        ``create_part`` call executes it once and stops. A successful
        ``modify_instance`` pauses the loop for exactly one optional
        ``inspect_instance`` verification step, then ends. A successful
        ``insert_asset`` automatically runs one ``inspect_instance``
        verification step (Phase 7D) and fails if the instance is not found or
        does not match the reported path/name/class. A ``build`` call (Phase 8A)
        enters multi-object build mode: action tools continue until the model
        sends a final report, and the system verifies every created path before
        reporting success.
        """
        messages = [
            providers.message("system", build_system_prompt(self.registry)),
            providers.message("user", prompt),
        ]
        steps = []
        last_text = None
        issued = 0
        # (Phase 6B) Set after a successful modify_instance so the model may run
        # exactly one optional inspect_instance verification step before the
        # loop ends. The reported action tool stays the modify_instance call.
        pending_verify = None
        # Track whether find_instances or inspect_instance was called during
        # this session, so we can conditionally enable verification after
        # create_part/create_script when the model has gathered context.
        inspection_called = False
        # Phase 8A build-mode state. ``build`` is an orchestration tool: when
        # the model calls it, the loop enters build mode so multiple action
        # tools can be executed and verified as one coherent build.
        build_mode = False
        build_plan = None
        build_paths = []
        build_failures = []
        # Phase 8B: track the accepted plan and how many planned steps have been
        # executed, plus scene inspections already performed so duplicate reads
        # can be skipped without a plugin round-trip.
        planned_steps = []
        plan_index = 0
        inspected_paths = {}
        # Phase 8D edit-mode state. ``edit_build`` is an orchestration tool: when
        # the model calls it, the loop enters edit mode so the Agent can modify
        # an existing build. Modified, newly-created, and deleted paths are
        # tracked for final verification.
        edit_mode = False
        edit_description = ""
        edit_modified_paths = []
        edit_created_paths = []
        edit_deleted_paths = []
        edit_failures = []
        # Paths that are safe targets for delete_instance in this request:
        # recent build context plus anything touched during this edit.
        edit_safe_delete_paths = {
            entry["path"]
            for entry in self.recent_build_context
            if isinstance(entry, dict) and entry.get("path")
        }
        effective_max_tool_calls = self.max_tool_calls

        while True:
            # -- model ------------------------------------------------------- #
            call_options = dict(chat_options)
            # Tool-capable providers (Groq, for GPT-OSS compatibility) receive
            # the registry's tool definitions so they never default tool_choice
            # to "none" while the prompt describes tools. The agent still only
            # parses JSON-in-text replies; backend-native tool calls (if any) are
            # normalized into that text by the provider. Ollama/mock are
            # unaffected (they do not set `supports_tools`).
            if getattr(self.provider, "supports_tools", False):
                call_options["tools"] = self.tool_definitions()
            try:
                response = self.provider.chat(messages, **call_options)
            except providers.ProviderError as exc:
                return AgentResult(
                    ok=False,
                    provider_text=last_text,
                    steps=steps,
                    error={
                        "code": "provider_error",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            last_text = response.text

            # -- parse ------------------------------------------------------- #
            try:
                reply = parse_agent_reply(last_text)
            except ToolCallParseError as exc:
                return AgentResult(
                    ok=False,
                    provider_text=last_text,
                    steps=steps,
                    error={"code": "malformed_output", "message": str(exc)},
                )

            if isinstance(reply, FinalMessage):
                # Phase 8A: a final report in build mode triggers a verification
                # pass over every created path. The build is only reported as
                # successful when every step succeeded and every path exists.
                if build_mode:
                    verified, verify_info = self._verify_build_paths(
                        build_paths, self.timeout
                    )
                    for entry in verify_info["paths"]:
                        issued += 1
                        steps.append(
                            {
                                "tool": "inspect_instance",
                                "arguments": {"path": entry["path"]},
                                "output": entry["ok"],
                                "data": entry,
                                "result": "verify {0}: {1}".format(
                                    entry["path"],
                                    "ok" if entry["ok"] else "missing",
                                ),
                                "ok": entry["ok"],
                            }
                        )
                    if build_failures or not verified:
                        messages = []
                        if build_failures:
                            messages.append(
                                "{0} build step(s) failed".format(len(build_failures))
                            )
                        if not verified:
                            messages.append(verify_info["message"])
                        return AgentResult(
                            ok=False,
                            provider_text=last_text,
                            steps=steps,
                            message=reply.text,
                            error={
                                "code": "build_failed",
                                "message": "; ".join(messages),
                            },
                        )
                    message = reply.text
                    if message and message.strip():
                        message = message.strip()
                    else:
                        message = self._build_summary(build_plan, build_paths)
                    # Phase 8D: remember the successfully-built objects so the
                    # next request can edit them naturally.
                    self._persist_build_context(build_paths, steps)
                    return AgentResult(
                        ok=True,
                        provider_text=last_text,
                        steps=steps,
                        message=message,
                    )
                # Phase 8D: a final report in edit mode triggers verification of
                # modified, newly-created, and deleted paths.
                if edit_mode:
                    verified, verify_info = self._verify_edit_paths(
                        edit_modified_paths,
                        edit_created_paths,
                        edit_deleted_paths,
                        self.timeout,
                    )
                    for entry in verify_info["paths"]:
                        issued += 1
                        steps.append(
                            {
                                "tool": "inspect_instance",
                                "arguments": {"path": entry["path"]},
                                "output": entry["ok"],
                                "data": entry,
                                "result": "verify {0}: {1}".format(
                                    entry["path"],
                                    "ok" if entry["ok"] else "unexpected state",
                                ),
                                "ok": entry["ok"],
                            }
                        )
                    if edit_failures or not verified:
                        messages = []
                        if edit_failures:
                            messages.append(
                                "{0} edit step(s) failed".format(len(edit_failures))
                            )
                        if not verified:
                            messages.append(verify_info["message"])
                        return AgentResult(
                            ok=False,
                            provider_text=last_text,
                            steps=steps,
                            message=reply.text,
                            error={
                                "code": "build_failed",
                                "message": "; ".join(messages),
                            },
                        )
                    message = reply.text
                    if message and message.strip():
                        message = message.strip()
                    else:
                        message = self._edit_summary(
                            edit_description,
                            edit_modified_paths,
                            edit_created_paths,
                            edit_deleted_paths,
                        )
                    return AgentResult(
                        ok=True,
                        provider_text=last_text,
                        steps=steps,
                        message=message,
                    )
                if pending_verify is not None:
                    return AgentResult(
                        ok=True,
                        tool=pending_verify["call"],
                        output=pending_verify["output"],
                        provider_text=last_text,
                        steps=steps,
                        message=reply.text,
                    )
                return AgentResult(
                    ok=True,
                    provider_text=last_text,
                    steps=steps,
                    message=reply.text,
                )
            call = reply

            # -- validate + execute through the registry only ---------------- #
            capturer = CapturingRBX(self.rbx)

            # Phase 6B guard: if pending_verify is already set and this call
            # matches the originally-executed action tool, skip re-execution
            # and reuse the stored output. This prevents double-execution when
            # the provider returns a scripted response that has already been
            # handled (e.g. RecordingProvider returning the same call text).
            skip_tool_execution = False
            cached_response_payload = None
            if (
                pending_verify is not None
                and call.name == pending_verify["call"].name
                and call.arguments == pending_verify["call"].arguments
            ):
                skip_tool_execution = True
                output = pending_verify["output"]

            # Phase 8B: in build mode, skip redundant inspect_instance calls for
            # the same path and reuse the cached result, saving plugin round-trips.
            if (
                not skip_tool_execution
                and build_mode
                and call.name == "inspect_instance"
            ):
                path = call.arguments.get("path")
                if path and path in inspected_paths:
                    skip_tool_execution = True
                    cached = inspected_paths[path]
                    output = cached["output"]
                    cached_response_payload = cached["response_payload"]

            # Phase 9A: analyze_scene is a read-only summarization tool. Compute
            # the summary directly on the raw connection so the internal
            # inspect_hierarchy/find_instances calls are not captured as the tool
            # result; only the compact summary is shown to the model.
            if call.name == "analyze_scene":
                summary = rbxforge.analyze_scene_summary(
                    self.rbx, call.arguments, self.timeout
                )
                skip_tool_execution = True
                failure = None
                if summary is None:
                    failure = {
                        "code": "execution_failed",
                        "message": "analyze_scene could not read the scene",
                    }
                    output = False
                else:
                    output = summary.get("result") or summary
                # Ensure the captured response payload stays None so the summary
                # dict is what gets compacted for the model.
                cached_response_payload = None

            if not skip_tool_execution:
                output = None
                failure = None
                # Phase 8D: delete_instance is only allowed on objects that are
                # part of the recent build context or have been touched during
                # this edit. This prevents arbitrary project deletion.
                if call.name == "delete_instance":
                    path = call.arguments.get("path")
                    if path and path not in edit_safe_delete_paths:
                        failure = {
                            "code": "invalid_deletion",
                            "message": (
                                "delete_instance is only allowed for objects in "
                                "the recent build context or touched in this edit"
                            ),
                        }
                if failure is None:
                    try:
                        output = self.registry.execute(
                            capturer, call.name, call.arguments, self.timeout
                        )
                    except rbxforge.UnknownToolError as exc:
                        failure = {"code": "unknown_tool", "message": str(exc)}
                    except rbxforge.InvalidParamsError as exc:
                        failure = {"code": "invalid_arguments", "message": str(exc)}
                if failure is None and not output:
                    failure = {
                        "code": "execution_failed",
                        "message": "tool {0!r} did not report success".format(
                            call.name
                        ),
                    }

            if not skip_tool_execution and failure is None and not output:
                failure = {
                    "code": "execution_failed",
                    "message": "tool {0!r} did not report success".format(call.name),
                }

            if cached_response_payload is not None:
                response_payload = cached_response_payload
            else:
                response_payload = (
                    capturer.responses[-1]["response"] if capturer.responses else None
                )

            # Phase 8B: cache successful scene inspections in build mode so a
            # later redundant read can be skipped.
            if (
                build_mode
                and call.name == "inspect_instance"
                and failure is None
                and output
            ):
                path = call.arguments.get("path")
                if path:
                    inspected_paths[path] = {
                        "output": output,
                        "response_payload": response_payload,
                    }
            compacted = (
                _compact_value(response_payload)
                if response_payload is not None
                else None
            )
            issued += 1
            steps.append(
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "output": output,
                    "data": compacted,
                    "result": compact_tool_result(call, output, response_payload),
                    "ok": failure is None,
                }
            )

            # Track if inspection tools were called, so we can conditionally
            # enable verification after create_part/create_script when the
            # model has gathered context.
            if call.name in ("find_instances", "inspect_instance"):
                inspection_called = True

            # Phase 8A: the build tool activates multi-object build mode. It is
            # an orchestration-only call: it does not change the project, but it
            # raises the per-request tool-call budget and allows subsequent
            # action tools to continue instead of ending the loop.
            if call.name == "build" and failure is None and output:
                build_mode = True
                build_plan = {
                    "description": call.arguments.get("description", ""),
                    "reference_path": call.arguments.get("reference_path"),
                }
                effective_max_tool_calls = max(
                    self.max_tool_calls, BUILD_MODE_MAX_TOOL_CALLS
                )
                messages.append(providers.message("assistant", last_text))
                messages.append(
                    providers.message(
                        "user",
                        tool_result_message(issued, call, output, response_payload),
                    )
                )
                continue

            # Phase 8B: plan_build validates and stores a structured construction
            # plan. It is only meaningful inside build mode; outside build mode it
            # is rejected so it cannot be used to bypass normal action-tool limits.
            if call.name == "plan_build":
                if failure is not None:
                    pass  # fall through to normal failure handling below
                elif not build_mode:
                    failure = {
                        "code": "invalid_plan",
                        "message": "plan_build must be called after build inside a multi-object build",
                    }
                elif output:
                    planned_steps = list(call.arguments.get("steps") or [])
                    plan_index = 0
                    messages.append(providers.message("assistant", last_text))
                    messages.append(
                        providers.message(
                            "user",
                            tool_result_message(
                                issued, call, output, response_payload
                            ),
                        )
                    )
                    continue

            # Phase 8B: advance through the planned steps when the model's actual
            # tool call matches the next expected step. This lets the Agent
            # notice when the plan has drifted and still records progress.
            if planned_steps and plan_index < len(planned_steps):
                expected = planned_steps[plan_index]
                if (
                    call.name == expected.get("tool")
                    and call.arguments == expected.get("arguments")
                ):
                    plan_index += 1

            # Phase 8D: edit_build activates iterative edit mode. Like build, it
            # is orchestration-only and raises the per-request budget.
            if call.name == "edit_build" and failure is None and output:
                edit_mode = True
                edit_description = call.arguments.get("description", "")
                effective_max_tool_calls = max(
                    self.max_tool_calls, BUILD_MODE_MAX_TOOL_CALLS
                )
                messages.append(providers.message("assistant", last_text))
                messages.append(
                    providers.message(
                        "user",
                        tool_result_message(issued, call, output, response_payload),
                    )
                )
                continue

            # Phase 8D: recent_build_context is read-only context for edits. It
            # is useful outside edit mode too, but it never ends the loop.
            if call.name == "recent_build_context" and failure is None and output:
                messages.append(providers.message("assistant", last_text))
                messages.append(
                    providers.message(
                        "user",
                        tool_result_message(issued, call, output, response_payload),
                    )
                )
                continue

            if failure is not None:
                if build_mode:
                    # In build mode a single step failure is recorded so the
                    # loop can continue (the model may abort with a final
                    # report), but the final report will report the build as
                    # failed rather than complete.
                    build_failures.append(
                        {"tool": call.name, "arguments": call.arguments, "error": failure}
                    )
                    messages.append(providers.message("assistant", last_text))
                    messages.append(
                        providers.message(
                            "user",
                            tool_result_message(issued, call, output, response_payload),
                        )
                    )
                    continue
                if edit_mode:
                    # In edit mode a single step failure is recorded so the
                    # model can abort cleanly with a final report.
                    edit_failures.append(
                        {"tool": call.name, "arguments": call.arguments, "error": failure}
                    )
                    messages.append(providers.message("assistant", last_text))
                    messages.append(
                        providers.message(
                            "user",
                            tool_result_message(issued, call, output, response_payload),
                        )
                    )
                    continue
                return AgentResult(
                    ok=False,
                    tool=call,
                    output=output,
                    provider_text=last_text,
                    steps=steps,
                    error=failure,
                )
            if call.name in ACTION_TOOLS:
                # Phase 8A: in build mode action tools are intermediate steps.
                # Record the created instance path and continue so the model
                # can execute the rest of the plan; final verification runs when
                # the model sends a final report.
                if build_mode:
                    path = self._extract_created_path(call.name, response_payload)
                    if path:
                        build_paths.append(path)
                    messages.append(providers.message("assistant", last_text))
                    messages.append(
                        providers.message(
                            "user",
                            tool_result_message(
                                issued, call, output, response_payload
                            ),
                        )
                    )
                    continue

                # Phase 8D: in edit mode action tools are intermediate steps.
                # Track modified, newly-created, and deleted paths so the final
                # verification pass can confirm the edit was applied correctly.
                if edit_mode:
                    path = None
                    if call.name == "modify_instance":
                        path = call.arguments.get("path")
                        if path and path not in edit_modified_paths:
                            edit_modified_paths.append(path)
                    elif call.name == "delete_instance":
                        path = call.arguments.get("path")
                        if path and path not in edit_deleted_paths:
                            edit_deleted_paths.append(path)
                    else:
                        path = self._extract_created_path(
                            call.name, response_payload
                        )
                        if path and path not in edit_created_paths:
                            edit_created_paths.append(path)
                    if path:
                        edit_safe_delete_paths.add(path)
                    messages.append(providers.message("assistant", last_text))
                    messages.append(
                        providers.message(
                            "user",
                            tool_result_message(
                                issued, call, output, response_payload
                            ),
                        )
                    )
                    continue

                # Phase 7D: insert_asset automatically verifies the placed
                # instance at the reported path before the loop ends. This makes
                # the end-to-end asset workflow reliable: the agent never claims
                # success when the instance cannot be found or does not match
                # the insertion report.
                if call.name == "insert_asset":
                    verified, verify_info = self._verify_inserted_asset(
                        response_payload, self.timeout
                    )
                    issued += 1
                    steps.append(
                        {
                            "tool": "inspect_instance",
                            "arguments": verify_info["arguments"],
                            "output": verify_info["output"],
                            "data": verify_info["data"],
                            "result": verify_info["result"],
                            "ok": verified,
                        }
                    )
                    if not verified:
                        return AgentResult(
                            ok=False,
                            tool=call,
                            output=output,
                            provider_text=last_text,
                            steps=steps,
                            error={
                                "code": "verification_failed",
                                "message": verify_info["message"],
                            },
                        )
                    return AgentResult(
                        ok=True,
                        tool=call,
                        output=output,
                        provider_text=last_text,
                        steps=steps,
                        message=verify_info["message"],
                    )

                # Phase 6B: a successful action tool does not end the loop
                # immediately - it permits exactly one optional inspect_instance
                # verification step before the loop ends. After a successful
                # mutation, the model may call inspect_instance to verify the
                # result when the target can be resolved and verification is
                # useful; verification is skipped when the tool result already
                # provides sufficient information.
                # modify_instance always permits verification; create_part and
                # create_script only permit it when the model has previously
                # called an inspection tool to gather project context.
                if pending_verify is None:
                    if call.name == "modify_instance":
                        pending_verify = {"call": call, "output": output}
                    elif (
                        call.name in ("create_part", "create_script")
                        and inspection_called
                    ):
                        pending_verify = {"call": call, "output": output}

                    if pending_verify is not None:
                        messages.append(providers.message("assistant", last_text))
                        messages.append(
                            providers.message(
                                "user",
                                tool_result_message(
                                    issued, call, output, response_payload
                                ),
                            )
                        )
                        continue
                return AgentResult(
                    ok=True,
                    tool=call,
                    output=output,
                    provider_text=last_text,
                    steps=steps,
                )
            if pending_verify is not None:
                # The optional verification inspect_instance (or any other
                # follow-up call) ends the loop here: no unrestricted autonomous
                # loop is created, and the modification remains the outcome.
                return AgentResult(
                    ok=True,
                    tool=pending_verify["call"],
                    output=pending_verify["output"],
                    provider_text=last_text,
                    steps=steps,
                )
            if issued >= effective_max_tool_calls:
                return AgentResult(
                    ok=False,
                    tool=call,
                    output=output,
                    provider_text=last_text,
                    steps=steps,
                    error={
                        "code": "max_tool_calls",
                        "message": "tool call budget exhausted after {0} call(s) "
                        "without completing the task".format(
                            effective_max_tool_calls
                        ),
                    },
                )

            # -- feed the bounded result back and continue ------------------- #
            messages.append(providers.message("assistant", last_text))
            messages.append(
                providers.message(
                    "user", tool_result_message(issued, call, output, response_payload)
                )
            )


def agent_from_env(
    registry=None, rbx=None, timeout=10.0, max_tool_calls=MAX_TOOL_CALLS
):
    """Build an :class:`Agent` with the provider configured from the environment
    (see :func:`providers.build_provider`; defaults to Ollama)."""
    return Agent(
        providers.build_provider(),
        registry=registry,
        rbx=rbx,
        timeout=timeout,
        max_tool_calls=max_tool_calls,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="rbxforge-agent",
        description="Run one natural-language prompt through the RBXForge agent. "
        "The provider is configured from the environment (see cli/providers.py).",
    )
    parser.add_argument("prompt", help='e.g. "create a red cube"')
    args = parser.parse_args()

    try:
        agent = agent_from_env()
    except providers.ProviderError as exc:
        print("provider error: {0}".format(exc))
        raise SystemExit(1)
    result = agent.run(args.prompt)
    if result.ok:
        print(
            "OK  {0!r} -> tool {1!r}: {2!r}".format(
                args.prompt, result.tool.name, result.output
            )
        )
    else:
        print("FAILED: {0}".format(result.error))
        raise SystemExit(1)
