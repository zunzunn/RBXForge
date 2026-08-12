#!/usr/bin/env python3
"""RBXForge AI provider layer - Phase 3A.

Provider-agnostic chat/inference interface (decision D-007). The agent (not yet
implemented) will depend on this interface, not on any specific provider. Today
this module provides:

- a stable ``Provider`` interface (``chat``) that all providers implement
- ``OllamaProvider`` ... the first implemented backend, using Ollama's local
  HTTP API (``POST {base_url}/api/chat``)
- ``GroqProvider`` ... a hosted backend using Groq's OpenAI-compatible chat API
  (``POST {base_url}/chat/completions``) authenticated with an API key
- ``NimProvider`` ... a recognized-but-not-implemented placeholder so NVIDIA NIM
  stays compatible with the design (decision D-009)
- ``MockProvider`` ... a deterministic provider for tests and local experiments
- configuration via environment variables and a ``build_provider`` factory

Standard library only (``urllib`` for the HTTP clients); no external
dependencies. No credentials are hard-coded. The model does not execute tools
yet and nothing here parses natural language into tool calls.
"""

import json
import os
import socket
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Provider errors
# --------------------------------------------------------------------------- #


class ProviderError(Exception):
    """Base class for provider-layer errors."""


class ProviderConfigError(ProviderError):
    """Raised when provider configuration is missing or invalid."""


class ProviderConnectionError(ProviderError):
    """Raised when the provider endpoint cannot be reached."""


class ProviderTimeoutError(ProviderConnectionError):
    """Raised when a provider request times out."""


class ProviderResponseError(ProviderError):
    """Raised when the provider returns an invalid or unexpected response."""


class ProviderNotImplementedError(ProviderError):
    """Raised when a provider is recognized but not implemented yet."""


#: Explicit User-Agent sent with every HTTP request. Hosted providers (e.g. Groq's
#: Cloudflare layer) block or rate-limit Python's default ``urllib`` User-Agent
#: (``403 code 1010``); an explicit, recognizable User-Agent avoids that.
RBXFORGE_USER_AGENT = "RBXForge/0.1.0"


# --------------------------------------------------------------------------- #
# Message / response types
# --------------------------------------------------------------------------- #


def message(role, content):
    """Build a chat message dict: ``{"role": ..., "content": ...}``."""
    return {"role": role, "content": content}


def _native_tool_calls_to_text(tool_calls):
    """Translate OpenAI/Groq-native ``message.tool_calls`` into the JSON-in-text
    the agent's ``parse_agent_reply`` expects (``{"tool", "arguments"}``).

    The agent loop is JSON-in-text by design and is not switched to native tool
    calling; this provider-side shim lets a tool-capable Groq model such as
    GPT-OSS call tools natively and still return the text the agent parses. Each
    tool call becomes one JSON object (newline-joined when several were emitted;
    the agent uses the first).
    """
    objects = []
    for call in tool_calls or []:
        function = call.get("function") or {}
        name = function.get("name")
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = None
        if not isinstance(arguments, dict):
            arguments = {"raw": raw_arguments}
        objects.append({"tool": name, "arguments": arguments})
    if not objects:
        return None
    if len(objects) == 1:
        return json.dumps(objects[0])
    return "\n".join(json.dumps(obj) for obj in objects)


class ProviderResponse:
    """A normalized chat/inference result, independent of the provider."""

    def __init__(self, text, model=None, provider=None, raw=None):
        self.text = text
        self.model = model
        self.provider = provider
        self.raw = raw

    def __repr__(self):
        return "ProviderResponse(provider={0!r}, model={1!r}, text={2!r})".format(
            self.provider, self.model, self.text
        )


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #


class Provider:
    """Base interface every provider implements.

    Providers are configured with their model name, base URL, optional API key,
    and request timeout. Credentials (API keys) are only ever read from
    configuration - never hard-coded here.
    """

    #: Machine name used to select this provider (e.g. "ollama").
    name = None

    def __init__(self, model, base_url=None, api_key=None, timeout=30.0):
        if not model:
            raise ProviderConfigError("a model name must be configured")
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, messages, **options):
        """Send a chat conversation and return a :class:`ProviderResponse`.

        ``messages`` is a list of ``{"role": ..., "content": ...}`` dicts (see
        :func:`message`). ``options`` are provider-specific generation options
        (temperature, max tokens, ...). Providers raise a :class:`ProviderError`
        subclass on any failure rather than swallowing it.
        """
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------- #

    def _check_api_key(self):
        if not self.api_key:
            raise ProviderConfigError(
                "{0} requires an API key; configure it via RBXFORGE_API_KEY "
                "(never hard-code credentials)".format(self.name)
            )

    def _http_json(self, url, body, headers=None):
        """POST ``body`` as JSON to ``url`` and return the decoded JSON response.

        Every request carries an explicit :data:`RBXFORGE_USER_AGENT` (hosted
        providers block Python's default ``urllib`` User-Agent — see the
        module constant). ``headers`` (optional) are extra request headers
        merged over the defaults (e.g. an ``Authorization`` header for hosted
        providers). Converts transport failures into the provider error
        hierarchy.
        """
        merged_headers = {
            "Content-Type": "application/json",
            "User-Agent": RBXFORGE_USER_AGENT,
        }
        if headers:
            merged_headers.update(headers)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=merged_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ProviderResponseError(
                "HTTP {0} from {1}: {2}".format(exc.code, self.name, detail)
            ) from exc
        except (TimeoutError, OSError) as exc:
            # TimeoutError/socket.timeout covers timeouts on all supported
            # Python versions; OSError (e.g. URLError) covers connection
            # refused / DNS failures.
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise ProviderTimeoutError(
                    "{0} request timed out after {1:g}s".format(self.name, self.timeout)
                ) from exc
            raise ProviderConnectionError(
                "{0} unreachable at {1}: {2}".format(self.name, url, exc)
            ) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ProviderResponseError(
                "{0} returned non-JSON output".format(self.name)
            ) from exc


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #

OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaProvider(Provider):
    """Local Ollama backend (decision D-008) via the ``/api/chat`` endpoint.

    Configuration:
      - model: any model pulled into Ollama (e.g. ``llama3.1``)
      - base_url: defaults to OLLAMA_DEFAULT_BASE_URL (no API key required)
    """

    name = "ollama"

    def __init__(
        self,
        model,
        base_url=OLLAMA_DEFAULT_BASE_URL,
        api_key=None,
        timeout=30.0,
    ):
        super().__init__(
            model,
            base_url=(base_url or OLLAMA_DEFAULT_BASE_URL),
            api_key=api_key,
            timeout=timeout,
        )

    def chat(self, messages, temperature=None, max_tokens=None, options=None, **extras):
        extra_option = dict(extras)
        if temperature is not None:
            extra_option["temperature"] = temperature
        if max_tokens is not None:
            extra_option["num_predict"] = max_tokens
        toptions = dict(options or {})
        toptions.update(extra_option)

        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if toptions:
            body["options"] = toptions

        data = self._http_json(self.base_url.rstrip("/") + "/api/chat", body)
        if isinstance(data, dict) and data.get("error"):
            raise ProviderResponseError(
                "ollama error: {0}".format(data["error"])
            )
        content = None
        if isinstance(data, dict):
            content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ProviderResponseError(
                "ollama response missing 'message.content'; got: {0!r}".format(data)
            )
        return ProviderResponse(
            text=content,
            model=data.get("model") or self.model,
            provider=self.name,
            raw=data,
        )


# --------------------------------------------------------------------------- #
# NVIDIA NIM (compatibility placeholder - not implemented)
# --------------------------------------------------------------------------- #


class NimProvider(Provider):
    """NVIDIA NIM backend placeholder (decision D-009).

    Recognized by the factory so the design stays NIM-compatible, but **not
    implemented yet**. Calling :meth:`chat` raises ProviderNotImplementedError.
    """

    name = "nim"

    def __init__(self, model, base_url=None, api_key=None, timeout=30.0):
        super().__init__(model, base_url, api_key, timeout)
        if not base_url:
            raise ProviderConfigError(
                "NIM requires RBXFORGE_BASE_URL (not implemented yet)"
            )

    def chat(self, messages, **options):
        raise ProviderNotImplementedError(
            "the NVIDIA NIM provider is recognized but not implemented "
            "(Groq and Ollama are implemented). "
            "Set RBXFORGE_PROVIDER=ollama to use a local model."
        )


# --------------------------------------------------------------------------- #
# Groq (hosted, OpenAI-compatible chat API)
# --------------------------------------------------------------------------- #

GROQ_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(Provider):
    """Groq backend via its OpenAI-compatible ``/chat/completions`` endpoint.

    Configuration:
      - model: any Groq-hosted chat model (e.g. ``llama-3.3-70b-versatile``)
      - base_url: defaults to GROQ_DEFAULT_BASE_URL
      - api_key: required; read from RBXFORGE_API_KEY (never hard-coded here).
        The key is passed only as the ``Authorization: Bearer ...`` header.

    ``chat`` stays on the plain-text JSON protocol the agent relies on: the
    request body is the OpenAI-compatible ``{model, messages, stream, ...}``
    shape and the content is read from ``choices[0].message.content``, so tool
    definitions and the agent's JSON-in-text parsing are unchanged.
    """

    name = "groq"

    #: Groq models accept OpenAI-style ``tools`` plus ``tool_choice``. The agent
    #: stays JSON-in-text; setting this flag makes the agent hand the registry's
    #: tool definitions to :meth:`chat` so Groq never defaults ``tool_choice`` to
    #: ``none`` (which turns GPT-OSS-style native tool calls into HTTP 400).
    supports_tools = True

    def __init__(
        self,
        model,
        base_url=GROQ_DEFAULT_BASE_URL,
        api_key=None,
        timeout=30.0,
    ):
        super().__init__(
            model,
            base_url=(base_url or GROQ_DEFAULT_BASE_URL),
            api_key=api_key,
            timeout=timeout,
        )
        self._check_api_key()

    def chat(self, messages, temperature=None, max_tokens=None, options=None,
             tools=None, **extras):
        extra_option = dict(extras)
        if temperature is not None:
            extra_option["temperature"] = temperature
        if max_tokens is not None:
            extra_option["max_tokens"] = max_tokens
        toptions = dict(options or {})
        toptions.update(extra_option)

        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        # Tool-capable Groq models (e.g. openai/gpt-oss-120b) call tools natively
        # whenever the prompt describes them. Groq defaults `tool_choice` to
        # "none" when no `tools` are supplied, so such a generation is rejected
        # with HTTP 400 "Tool choice is none, but model called a tool" (code
        # tool_use_failed) - exactly the observed GPT-OSS failure. Supplying the
        # real definitions with `tool_choice: "auto"` allows the call; native
        # tool calls are then normalized back into the JSON-in-text the agent
        # parses (see `_native_tool_calls_to_text`), so the JSON-in-text agent
        # architecture is unchanged.
        if tools:
            if "tool_choice" not in toptions:
                toptions["tool_choice"] = "auto"
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters")
                        or {"type": "object", "properties": {}},
                    },
                }
                for tool in tools
            ]
        body.update(toptions)

        data = self._http_json(
            self.base_url.rstrip("/") + "/chat/completions",
            body,
            headers={"Authorization": "Bearer {0}".format(self.api_key)},
        )
        if isinstance(data, dict) and data.get("error"):
            raise ProviderResponseError(
                "groq error: {0}".format(data["error"])
            )
        content = None
        native_text = None
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message") or {}
                content = message.get("content")
                if message.get("tool_calls"):
                    native_text = _native_tool_calls_to_text(message["tool_calls"])
        # A native tool call takes precedence over any stray content (Groq tends
        # to leave content empty or whitespace when it calls a tool).
        if native_text is not None:
            content = native_text
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError(
                "groq response missing usable text; got: {0!r}".format(data)
            )
        return ProviderResponse(
            text=content,
            model=data.get("model") or self.model,
            provider=self.name,
            raw=data,
        )


# --------------------------------------------------------------------------- #
# Mock (tests / local experiments)
# --------------------------------------------------------------------------- #


class MockProvider(Provider):
    """Deterministic in-process provider for tests.

    ``response_text`` is returned verbatim from ``chat`` unless ``fail`` is set
    to ``"timeout"``, ``"connection"``, or ``"response"``, in which case the
    matching provider error is raised.
    """

    name = "mock"

    def __init__(
        self,
        model="mock-model",
        base_url=None,
        api_key=None,
        timeout=30.0,
        response_text="",
        fail=None,
    ):
        super().__init__(model, base_url, api_key, timeout)
        self.response_text = response_text if response_text is not None else ""
        self.fail = fail

    def chat(self, messages, **options):
        if self.fail == "timeout":
            raise ProviderTimeoutError("mock provider timed out")
        if self.fail == "connection":
            raise ProviderConnectionError("mock provider unreachable")
        if self.fail == "response":
            raise ProviderResponseError("mock provider returned bad data")
        return ProviderResponse(
            text=self.response_text,
            model=self.model,
            provider=self.name,
            raw={"mock": True, "options": options or None},
        )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_PROVIDER = "ollama"
DEFAULT_TIMEOUT = 30.0

#: Environment variables consumed by :func:`ProviderSettings.from_env`, plus the
#: mock-only testing knobs (:data:`RBXFORGE_MOCK_RESPONSE` / :data:`RBXFORGE_MOCK_FAIL`
#: are read by :func:`build_provider` for the mock provider so end-to-end/REPL tests
#: can drive a deterministic subprocess without a real model).
CONFIG_ENV_VARS = (
    "RBXFORGE_PROVIDER",
    "RBXFORGE_MODEL",
    "RBXFORGE_BASE_URL",
    "RBXFORGE_API_KEY",
    "RBXFORGE_TIMEOUT",
    "RBXFORGE_MOCK_RESPONSE",
    "RBXFORGE_MOCK_FAIL",
)


class ProviderSettings:
    """Resolved provider configuration.

    Can be constructed directly or read from the environment with
    ``from_env()``. Values passed to the constructor win over the environment.
    """

    def __init__(
        self,
        provider=None,
        model=None,
        base_url=None,
        api_key=None,
        timeout=None,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

        value = self.timeout
        if value is None:
            default = os.environ.get("RBXFORGE_TIMEOUT", "")
            value = default if default != "" else DEFAULT_TIMEOUT
        self.timeout = _parse_timeout(value)

    @classmethod
    def from_env(cls, env=None):
        """Read configuration from ``env`` (defaults to ``os.environ``)."""
        env = os.environ if env is None else env
        return cls(
            provider=env.get("RBXFORGE_PROVIDER"),
            model=env.get("RBXFORGE_MODEL"),
            base_url=env.get("RBXFORGE_BASE_URL"),
            api_key=env.get("RBXFORGE_API_KEY"),
            timeout=env.get("RBXFORGE_TIMEOUT"),
        )

    def select_provider(self):
        """Resolve the provider name; ``RBXFORGE_PROVIDER`` defaults to ollama."""
        name = (self.provider or DEFAULT_PROVIDER).strip().lower()
        if not name:
            name = DEFAULT_PROVIDER
        return name


def _parse_timeout(value):
    if value is None:
        return DEFAULT_TIMEOUT
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ProviderConfigError("RBXFORGE_TIMEOUT must be a number, got: {0!r}".format(value))
    if parsed <= 0:
        raise ProviderConfigError("RBXFORGE_TIMEOUT must be positive, got: {0!r}".format(value))
    return parsed


def build_provider(settings=None):
    """Select and build a provider from settings (or the environment).

    Returns a :class:`Provider` instance. Raises :class:`ProviderConfigError`
    for an unknown provider or missing required configuration, and
    :class:`ProviderNotImplementedError` for recognized-but-unimplemented
    providers (currently ``nim``).
    """
    settings = settings if settings is not None else ProviderSettings.from_env()
    name = settings.select_provider()
    model = settings.model
    if not model:
        raise ProviderConfigError(
            "no model configured: set RBXFORGE_MODEL (e.g. llama3.1 for Ollama)"
        )

    if name == "ollama":
        return OllamaProvider(
            model=model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
        )
    if name == "groq":
        return GroqProvider(
            model=model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
        )
    if name == "nim":
        return NimProvider(
            model=model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
        )
    if name in ("mock", "fake"):
        return MockProvider(
            model=model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
            # RBXFORGE_MOCK_RESPONSE / RBXFORGE_MOCK_FAIL let end-to-end tests
            # (interactive REPL, subprocess) drive the mock deterministically.
            response_text=os.environ.get("RBXFORGE_MOCK_RESPONSE", ""),
            fail=os.environ.get("RBXFORGE_MOCK_FAIL") or None,
        )
    raise ProviderConfigError(
        "unknown provider: {0!r}. Supported providers: ollama, groq, nim (not "
        "implemented), mock. Set RBXFORGE_PROVIDER.".format(settings.provider)
    )


if __name__ == "__main__":
    # Manual smoke check: configure from the environment and print the selected
    # provider (without contacting the network).
    import argparse

    parser = argparse.ArgumentParser(
        prog="rbxforge-providers",
        description="Show which AI provider would be selected from the environment.",
    )
    parser.add_argument(
        "--provider", default=None, help="override RBXFORGE_PROVIDER"
    )
    args = parser.parse_args()

    settings = ProviderSettings.from_env()
    if args.provider:
        settings.provider = args.provider
    try:
        provider = build_provider(settings)
    except ProviderError as exc:
        print("provider error: {0}".format(exc))
        raise SystemExit(1)
    print("selected provider: {0} (model={1!r}, base_url={2!r}, timeout={3:g}s)".format(
        provider.name,
        provider.model,
        getattr(provider, "base_url", None),
        provider.timeout,
    ))