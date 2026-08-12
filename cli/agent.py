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
step. It eventually executes an action tool (create_part), at which point the
loop stops and a concise final AgentResult is returned.

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

import rbxforge   # Phase 2B tool layer (Tool, ToolRegistry, ...) - noqa: E402
import providers  # Phase 3A provider layer (Provider, ProviderError, ...) - noqa: E402


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
                return text[start:index + 1]
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
        if isinstance(data["message"], str) and data["message"].strip():
            return FinalMessage(data["message"].strip())
        raise ToolCallParseError("a final 'message' must be a non-empty string")
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

#: Tool names that change the project. Calling an action tool ends the loop.
ACTION_TOOLS = frozenset({"create_part"})

#: Hard bound on executed tool calls per user request.
MAX_TOOL_CALLS = 5

#: Bounds applied to tool results before they are shown to the model.
MAX_TOOL_RESULT_ITEMS = 20       # cap on list/dict entries (e.g. matches, children)
MAX_TOOL_RESULT_STRING = 200     # per-string truncation length
MAX_TOOL_RESULT_CHARS = 2000     # serialized result budget


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
        return [_compact_value(item, depth + 1) for item in value[:MAX_TOOL_RESULT_ITEMS]]
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
        text = repr(output)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        suffix = "...\n[result truncated for length]"
        keep = max(0, MAX_TOOL_RESULT_CHARS - len(suffix))
        text = text[:keep] + suffix
    return text


def tool_result_message(index, call, output, response_payload):
    """The message appended after a tool executes, so the next model turn can
    act on what actually happened in Studio."""
    return (
        "Tool call #{0}: {1}\n"
        "Arguments: {2}\n"
        "Result: {3}".format(
            index,
            call.name,
            json.dumps(call.arguments, sort_keys=True),
            compact_tool_result(call, output, response_payload),
        )
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
        "Available tools:\n"
        + tools_json
        + "\n"
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
        "- create_part changes the project; once a change tool reports success, "
        "the task is complete - stop, do not call more tools.\n"
        "- If a request is already simple (e.g. 'create a red cube'), make the "
        "single tool call you need immediately instead of exploring.\n"
        "- If you conclude no tool call is needed, reply with a final report.\n"
        "- 'arguments' must satisfy the selected tool's parameters schema "
        "exactly.\n"
        "3D vectors (e.g. position, size) are JSON objects of the form "
        '{"x": number, "y": number, "z": number} - never arrays like [0,5,0] '
        'and never strings like "0,5,0".\n'
        'Example call:\n'
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
    - ``tool``: the last executed :class:`ToolCall`, or None when no tool
      executed (a final-report completion).
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

    def __init__(self, ok, tool=None, output=None, error=None, provider_text=None,
                 message=None, steps=None):
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

    Phase 4D; preserves the Phase 3B single-step behavior for simple requests.
    The loop is bounded per user request by ``max_tool_calls`` (default 5), and
    only compacted, bounded tool results are ever returned to the model.

    - ``provider``: a Phase 3A ``Provider`` instance (Ollama, mock, ...).
    - ``registry``: the :class:`ToolRegistry` tools are validated and executed
      through (defaults to the built-in registry).
    - ``rbx``: the object handed to the registry when executing a tool - an
      ``RBXForge`` connection in real use, a fake in tests.
    - ``timeout``: per-tool execution timeout in seconds.
    - ``max_tool_calls``: hard bound on tool calls per request.
    """

    def __init__(self, provider, registry=None, rbx=None, timeout=10.0,
                 max_tool_calls=MAX_TOOL_CALLS):
        self.provider = provider
        self.registry = registry if registry is not None else rbxforge.default_registry()
        self.rbx = rbx if rbx is not None else rbxforge.RBXForge()
        self.timeout = timeout
        self.max_tool_calls = max_tool_calls

    def tool_definitions(self):
        """The currently registered tool definitions sent to the model."""
        return tool_definitions(self.registry)

    def run(self, prompt, **chat_options):
        """Run ``prompt`` through the bounded multi-step loop.

        Returns a concise :class:`AgentResult`. Provider failures, malformed
        model output, unknown tools, invalid arguments, execution failures, and
        hitting the per-request tool-call budget are all returned as failures
        (never raised), so this is safe to call from the REPL or CLI.

        Single-step requests behave as before: a model that replies with a
        ``create_part`` call executes it once and stops.
        """
        messages = [
            providers.message("system", build_system_prompt(self.registry)),
            providers.message("user", prompt),
        ]
        steps = []
        last_text = None
        issued = 0

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
                return AgentResult(
                    ok=True,
                    provider_text=last_text,
                    steps=steps,
                    message=reply.text,
                )
            call = reply

            # -- validate + execute through the registry only ---------------- #
            capturer = CapturingRBX(self.rbx)
            output = None
            failure = None
            try:
                output = self.registry.execute(capturer, call.name, call.arguments,
                                               self.timeout)
            except rbxforge.UnknownToolError as exc:
                failure = {"code": "unknown_tool", "message": str(exc)}
            except rbxforge.InvalidParamsError as exc:
                failure = {"code": "invalid_arguments", "message": str(exc)}
            if failure is None and not output:
                failure = {
                    "code": "execution_failed",
                    "message": "tool {0!r} did not report success".format(call.name),
                }

            response_payload = (
                capturer.responses[-1]["response"] if capturer.responses else None
            )
            compacted = (
                _compact_value(response_payload) if response_payload is not None else None
            )
            issued += 1
            steps.append({
                "tool": call.name,
                "arguments": call.arguments,
                "output": output,
                "data": compacted,
                "result": compact_tool_result(call, output, response_payload),
                "ok": failure is None,
            })

            if failure is not None:
                return AgentResult(
                    ok=False,
                    tool=call,
                    output=output,
                    provider_text=last_text,
                    steps=steps,
                    error=failure,
                )
            if call.name in ACTION_TOOLS:
                return AgentResult(
                    ok=True,
                    tool=call,
                    output=output,
                    provider_text=last_text,
                    steps=steps,
                )
            if issued >= self.max_tool_calls:
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
                                       self.max_tool_calls),
                    },
                )

            # -- feed the bounded result back and continue ------------------- #
            messages.append(providers.message("assistant", last_text))
            messages.append(providers.message(
                "user", tool_result_message(issued, call, output, response_payload)
            ))


def agent_from_env(registry=None, rbx=None, timeout=10.0,
                   max_tool_calls=MAX_TOOL_CALLS):
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
        print("OK  {0!r} -> tool {1!r}: {2!r}".format(
            args.prompt, result.tool.name, result.output
        ))
    else:
        print("FAILED: {0}".format(result.error))
        raise SystemExit(1)