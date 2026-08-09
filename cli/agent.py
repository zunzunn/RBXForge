#!/usr/bin/env python3
"""RBXForge minimal agent - Phase 3B: prompt -> provider -> tool call -> execution.

Connects the Phase 3A provider layer (cli/providers.py) to the Phase 2B tool
layer (cli/rbxforge.py). A single natural-language prompt is sent to a provider
together with the currently registered tool definitions. The model is expected
to reply with a structured tool call:

    {"tool": "<tool name>", "arguments": { ... }}

The call is parsed, validated against the ToolRegistry, and executed through the
registry only. Unknown tools and invalid arguments are rejected safely instead
of being executed.

Explicitly out of scope for Phase 3B (no multi-step autonomous loops, no
conversation history, no project inspection, no plugin/protocol changes, no new
Studio tools). The Studio plugin and WebSocket protocol are untouched.

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
    """Build the system message describing available tools and the required
    output format."""
    tools_json = json.dumps(tool_definitions(registry))
    return (
        "You are the RBXForge building agent. You can call exactly one RBXForge tool.\n"
        "Available tools:\n"
        + tools_json
        + "\n"
        "Reply with only a single JSON object selecting a tool and its arguments:\n"
        '{"tool": "<tool name>", "arguments": { ... }}\n'
        "'arguments' must satisfy the selected tool's parameters schema exactly; "
        "every declared property is required.\n"
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
    """Outcome of a single :meth:`Agent.run` call.

    - ``ok``: True only when a valid tool call was executed successfully.
    - ``tool``: the parsed :class:`ToolCall`, or None when it could not be parsed.
    - ``output``: the raw tool execution result from the tool layer (e.g. bool).
    - ``error``: None, or ``{"code": ..., "message": ...}`` (+ ``type`` for
      provider errors).
    - ``provider_text``: the raw provider text, for diagnostics.
    """

    def __init__(self, ok, tool=None, output=None, error=None, provider_text=None):
        self.ok = ok
        self.tool = tool
        self.output = output
        self.error = error
        self.provider_text = provider_text

    def __repr__(self):
        return "AgentResult(ok={0!r}, error={1!r}, tool={2!r})".format(
            self.ok, self.error, self.tool
        )


class Agent:
    """Minimal single-step agent: prompt -> provider -> tool call -> execution.

    Phase 3B deliberately performs **one** tool call per prompt and returns. No
    multi-step loop, no memory, no project inspection.

    - ``provider``: a Phase 3A ``Provider`` instance (Ollama, mock, ...).
    - ``registry``: the :class:`ToolRegistry` tools are validated and executed
      through (defaults to the built-in registry with ``create_part``).
    - ``rbx``: the object handed to the registry when executing a tool - an
      ``RBXForge`` connection in real use, a fake in tests.
    - ``timeout``: per-tool execution timeout in seconds.
    """

    def __init__(self, provider, registry=None, rbx=None, timeout=10.0):
        self.provider = provider
        self.registry = registry if registry is not None else rbxforge.default_registry()
        self.rbx = rbx if rbx is not None else rbxforge.RBXForge()
        self.timeout = timeout

    def tool_definitions(self):
        """The currently registered tool definitions sent to the model."""
        return tool_definitions(self.registry)

    def run(self, prompt, **chat_options):
        """Run one prompt through the provider and execute the resulting call.

        Returns an :class:`AgentResult`. Provider failures, malformed model
        output, unknown tools, and invalid arguments are all returned as
        failures (never raised), so this is safe to call from the REPL or CLI.
        """
        # -- prompt -> provider ------------------------------------------------ #
        messages = [
            providers.message("system", build_system_prompt(self.registry)),
            providers.message("user", prompt),
        ]
        try:
            response = self.provider.chat(messages, **chat_options)
        except providers.ProviderError as exc:
            return AgentResult(
                ok=False,
                error={
                    "code": "provider_error",
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        text = response.text

        # -- provider output -> structured tool call --------------------------- #
        try:
            call = parse_tool_call(text)
        except ToolCallParseError as exc:
            return AgentResult(
                ok=False,
                provider_text=text,
                error={"code": "malformed_output", "message": str(exc)},
            )

        # -- validate + execute through the registry only ---------------------- #
        try:
            output = self.registry.execute(self.rbx, call.name, call.arguments, self.timeout)
        except rbxforge.UnknownToolError as exc:
            return AgentResult(
                ok=False,
                tool=call,
                provider_text=text,
                error={"code": "unknown_tool", "message": str(exc)},
            )
        except rbxforge.InvalidParamsError as exc:
            return AgentResult(
                ok=False,
                tool=call,
                provider_text=text,
                error={"code": "invalid_arguments", "message": str(exc)},
            )
        if output:
            return AgentResult(ok=True, tool=call, output=output, provider_text=text)
        return AgentResult(
            ok=False,
            tool=call,
            output=output,
            provider_text=text,
            error={
                "code": "execution_failed",
                "message": "tool {0!r} did not report success".format(call.name),
            },
        )


def agent_from_env(registry=None, rbx=None, timeout=10.0):
    """Build an :class:`Agent` with the provider configured from the environment
    (see :func:`providers.build_provider`; defaults to Ollama)."""
    return Agent(
        providers.build_provider(),
        registry=registry,
        rbx=rbx,
        timeout=timeout,
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