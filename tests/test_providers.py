#!/usr/bin/env python3
"""Provider-layer tests for the RBXForge AI abstraction (Phase 3A + Phase 4E).

Covers provider selection, environment/configuration, the Ollama HTTP client
(against a fake in-process Ollama /api/chat server), the Groq provider (against
a fake OpenAI-compatible /chat/completions server), timeouts/errors, response
parsing, and the mock provider. Standard library only.

Run from the repository root:
    python3 tests/test_providers.py
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROVIDERS = os.path.join(ROOT, "cli", "providers.py")

_PROVIDERS_MODULE = None


def load_providers():
    global _PROVIDERS_MODULE
    if _PROVIDERS_MODULE is None:
        spec = importlib.util.spec_from_file_location("rbxforge_providers", PROVIDERS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PROVIDERS_MODULE = module
    return _PROVIDERS_MODULE


_AGENT_MODULE = None
AGENT = os.path.join(ROOT, "cli", "agent.py")


def load_agent_module():
    global _AGENT_MODULE
    if _AGENT_MODULE is None:
        spec = importlib.util.spec_from_file_location("rbxforge_agent", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _AGENT_MODULE = module
    return _AGENT_MODULE


# --------------------------------------------------------------------------- #
# A fake Ollama /api/chat server
# --------------------------------------------------------------------------- #


class FakeOllamaHandler(BaseHTTPRequestHandler):
    mode = "ok"        # ok | error | http500 | sleep
    bodies = []
    user_agents = []

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        FakeOllamaHandler.bodies.append(self._read_body())
        FakeOllamaHandler.user_agents.append(self.headers.get("User-Agent"))
        if self.path != "/api/chat":
            self._send(404, {"error": "unexpected path: " + self.path})
        elif self.mode == "error":
            self._send(200, {"error": "model 'nope' not found"})
        elif self.mode == "http500":
            self._send(500, {"error": "server exploded"})
        elif self.mode == "sleep":
            time.sleep(2.0)
            self._send(200, {"model": "slow", "message": {"content": "late reply"}})
        else:
            self._send(200, {
                "model": "fake-ollama",
                "message": {"content": "hello from fake ollama"},
            })

    def log_message(self, format, *args):
        pass


class FakeOllamaServer:
    def __init__(self, mode="ok"):
        FakeOllamaHandler.bodies = []
        FakeOllamaHandler.user_agents = []
        FakeOllamaHandler.mode = mode
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()   # released; used to point at a closed port
    return port


# --------------------------------------------------------------------------- #
# A fake Groq OpenAI-compatible /chat/completions server
# --------------------------------------------------------------------------- #


class FakeGroqHandler(BaseHTTPRequestHandler):
    mode = "ok"   # ok | error | http500 | sleep | missing_content | empty_choices | nonjson
    bodies = []
    auth_headers = []
    user_agents = []

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_raw(self, code, text):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        FakeGroqHandler.bodies.append(self._read_body())
        FakeGroqHandler.auth_headers.append(self.headers.get("Authorization"))
        FakeGroqHandler.user_agents.append(self.headers.get("User-Agent"))
        if self.path != "/chat/completions":
            self._send_json(404, {"error": "unexpected path: " + self.path})
        elif self.mode == "error":
            self._send_json(200, {"error": "model 'nope' not found"})
        elif self.mode == "http500":
            self._send_json(500, {"error": "server exploded"})
        elif self.mode == "sleep":
            time.sleep(2.0)
            self._send_json(200, {
                "model": "slow",
                "choices": [{"message": {"role": "assistant", "content": "late reply"}}],
            })
        elif self.mode == "gptoss_native_tool":
            # Reproduces the real GPT-OSS failure mode on Groq: the model emits a
            # NATIVE OpenAI-style tool call (content empty) instead of the
            # JSON-in-text the agent asks for.
            self._send_json(200, {
                "model": "gpt-oss-120b",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_rbx_1",
                            "type": "function",
                            "function": {
                                "name": "find_instances",
                                "arguments": '{"query": "SpawnLocation", "max_results": 10}',
                            },
                        }],
                    },
                }],
            })
        elif self.mode == "missing_content":
            self._send_json(200, {"model": "fake-groq", "choices": [{"message": {}}]})
        elif self.mode == "empty_choices":
            self._send_json(200, {"model": "fake-groq", "choices": []})
        elif self.mode == "nonjson":
            self._send_raw(200, "this is not json")
        else:
            self._send_json(200, {
                "model": "fake-groq",
                "choices": [
                    {"message": {"role": "assistant", "content": "hello from fake groq"}},
                ],
            })

    def log_message(self, format, *args):
        pass


class FakeGroqServer:
    def __init__(self, mode="ok"):
        FakeGroqHandler.bodies = []
        FakeGroqHandler.auth_headers = []
        FakeGroqHandler.user_agents = []
        FakeGroqHandler.mode = mode
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGroqHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #
# Mock provider
# --------------------------------------------------------------------------- #


def scenario_mock_success():
    mod = load_providers()
    provider = mod.MockProvider(model="mock-m", response_text="the answer is 42")
    response = provider.chat([mod.message("user", "what is 42*42?")])
    assert response.provider == "mock", response
    assert response.model == "mock-m", response
    assert response.text == "the answer is 42", response
    print("OK  mock provider returns configured text")


def scenario_mock_failures():
    mod = load_providers()
    expectations = [
        ("timeout", mod.ProviderTimeoutError),
        ("connection", mod.ProviderConnectionError),
        ("response", mod.ProviderResponseError),
    ]
    for fail, exc_type in expectations:
        provider = mod.MockProvider(model="mock-m", fail=fail)
        try:
            provider.chat([mod.message("user", "hi")])
        except exc_type:
            pass
        else:
            raise AssertionError("mock fail={0!r} did not raise {1}".format(fail, exc_type.__name__))
    print("OK  mock provider raises timeout/connection/response errors on demand")


# --------------------------------------------------------------------------- #
# Provider selection & configuration
# --------------------------------------------------------------------------- #


def scenario_provider_selection():
    mod = load_providers()
    settings = mod.ProviderSettings(provider="ollama", model="llama3.1")
    provider = mod.build_provider(settings)
    assert isinstance(provider, mod.OllamaProvider), provider

    settings = mod.ProviderSettings(provider="mock", model="mock-m")
    provider = mod.build_provider(settings)
    assert isinstance(provider, mod.MockProvider), provider

    # NIM is recognized but not implemented.
    settings = mod.ProviderSettings(provider="nim", model="nim-model", base_url="http://nim:8000")
    provider = mod.build_provider(settings)
    assert isinstance(provider, mod.NimProvider), provider
    try:
        provider.chat([mod.message("user", "hi")])
    except mod.ProviderNotImplementedError:
        pass
    else:
        raise AssertionError("nim chat should raise ProviderNotImplementedError")

    # Unknown provider -> clear config error.
    try:
        mod.build_provider(mod.ProviderSettings(provider="bogus", model="m"))
    except mod.ProviderConfigError:
        pass
    else:
        raise AssertionError("unknown provider should raise ProviderConfigError")

    # Provider name defaults to ollama.
    defaults = mod.ProviderSettings(model="llama3.1")
    assert defaults.select_provider() == "ollama", defaults.select_provider()
    print("OK  provider selection (ollama/mock/nim/unknown/default)")


def scenario_configuration_env():
    mod = load_providers()
    env = {
        "RBXFORGE_PROVIDER": "ollama",
        "RBXFORGE_MODEL": "llama3.1",
        "RBXFORGE_BASE_URL": "http://127.0.0.1:9999",
        "RBXFORGE_TIMEOUT": "7",
    }
    settings = mod.ProviderSettings.from_env(env)
    assert settings.provider == "ollama"
    assert settings.model == "llama3.1"
    assert settings.base_url == "http://127.0.0.1:9999"
    assert settings.timeout == 7.0

    provider = mod.build_provider(settings)
    assert isinstance(provider, mod.OllamaProvider)
    assert provider.base_url == "http://127.0.0.1:9999"
    assert provider.timeout == 7.0
    assert provider.model == "llama3.1"
    assert provider.api_key is None

    # Constructor values win over env.
    settings = mod.ProviderSettings(model="explicit", timeout=11, provider="mock")
    assert settings.model == "explicit"
    assert settings.timeout == 11.0
    assert settings.select_provider() == "mock"
    print("OK  configuration read from environment and explicit values")


def scenario_configuration_errors():
    mod = load_providers()
    # Missing model -> config error (model is never hard-coded).
    try:
        mod.build_provider(mod.ProviderSettings(provider="ollama"))
    except mod.ProviderConfigError:
        pass
    else:
        raise AssertionError("missing model should raise ProviderConfigError")

    # Set OLLAMA defaults.
    provider = mod.OllamaProvider(model="llama3.1")
    assert provider.base_url == mod.OLLAMA_DEFAULT_BASE_URL, provider.base_url
    assert provider.api_key is None

    # Bad timeout -> config error.
    try:
        mod.ProviderSettings(timeout="abc")
    except mod.ProviderConfigError:
        pass
    else:
        raise AssertionError("bad timeout should raise ProviderConfigError")
    try:
        mod.ProviderSettings(timeout="-1")
    except mod.ProviderConfigError:
        pass
    else:
        raise AssertionError("negative timeout should raise ProviderConfigError")
    print("OK  configuration errors (missing model, bad timeout, ollama defaults)")


# --------------------------------------------------------------------------- #
# Ollama HTTP client against the fake server
# --------------------------------------------------------------------------- #


def scenario_ollama_success():
    mod = load_providers()
    with FakeOllamaServer(mode="ok") as server:
        provider = mod.OllamaProvider(
            model="llama3.1",
            base_url="http://127.0.0.1:{0}".format(server.port),
            timeout=5.0,
        )
        response = provider.chat(
            [mod.message("user", "make a red cube")],
            temperature=0.2,
            max_tokens=200,
        )
    assert response.provider == "ollama", response
    assert response.text == "hello from fake ollama", response
    assert response.model == "fake-ollama", response
    assert len(FakeOllamaHandler.bodies) == 1, FakeOllamaHandler.bodies
    body = json.loads(FakeOllamaHandler.bodies[0])
    assert body["model"] == "llama3.1", body
    assert body["stream"] is False, body
    assert body["messages"][0] == {"role": "user", "content": "make a red cube"}, body
    assert body["options"]["temperature"] == 0.2, body
    assert body["options"]["num_predict"] == 200, body
    assert FakeOllamaHandler.user_agents == [mod.RBXFORGE_USER_AGENT], \
        FakeOllamaHandler.user_agents
    assert mod.RBXFORGE_USER_AGENT == "RBXForge/0.1.0", mod.RBXFORGE_USER_AGENT
    print("OK  ollama provider success; request body + User-Agent verified")


def scenario_ollama_timeout():
    mod = load_providers()
    with FakeOllamaServer(mode="sleep") as server:
        provider = mod.OllamaProvider(
            model="llama3.1",
            base_url="http://127.0.0.1:{0}".format(server.port),
            timeout=0.3,
        )
        try:
            provider.chat([mod.message("user", "hi")])
        except mod.ProviderTimeoutError:
            pass
        else:
            raise AssertionError("expected ProviderTimeoutError")
    print("OK  ollama provider times out")


def scenario_ollama_connection_error():
    mod = load_providers()
    provider = mod.OllamaProvider(
        model="llama3.1",
        base_url="http://127.0.0.1:{0}".format(free_port()),
        timeout=0.5,
    )
    try:
        provider.chat([mod.message("user", "hi")])
    except mod.ProviderConnectionError:
        pass
    else:
        raise AssertionError("expected ProviderConnectionError")
    print("OK  ollama connection refused -> ProviderConnectionError")


def scenario_ollama_response_errors():
    mod = load_providers()
    with FakeOllamaServer(mode="error") as server:
        provider = mod.OllamaProvider(
            model="llama3.1",
            base_url="http://127.0.0.1:{0}".format(server.port),
            timeout=5.0,
        )
        try:
            provider.chat([mod.message("user", "hi")])
        except mod.ProviderResponseError:
            pass
        else:
            raise AssertionError("expected ProviderResponseError from error payload")

    with FakeOllamaServer(mode="http500") as server:
        provider = mod.OllamaProvider(
            model="llama3.1",
            base_url="http://127.0.0.1:{0}".format(server.port),
            timeout=5.0,
        )
        try:
            provider.chat([mod.message("user", "hi")])
        except mod.ProviderResponseError:
            pass
        else:
            raise AssertionError("expected ProviderResponseError from HTTP 500")
    print("OK  ollama error payload and HTTP error -> ProviderResponseError")


def scenario_groq_selection_and_config():
    """RBXFORGE_PROVIDER=groq must select GroqProvider with the configured
    model/base_url/api_key/timeout; the key is never hard-coded (missing key is a
    config error) and the default base URL is Groq's API endpoint."""
    mod = load_providers()
    settings = mod.ProviderSettings(
        provider="groq",
        model="llama-3.3-70b-versatile",
        base_url="http://127.0.0.1:9",
        api_key="sk-test-42",
        timeout=7,
    )
    provider = mod.build_provider(settings)
    assert isinstance(provider, mod.GroqProvider), provider
    assert provider.model == "llama-3.3-70b-versatile", provider.model
    assert provider.base_url == "http://127.0.0.1:9", provider.base_url
    assert provider.api_key == "sk-test-42", provider.api_key
    assert provider.timeout == 7.0, provider.timeout

    direct = mod.GroqProvider(model="llama-3.3-70b-versatile", api_key="sk-test-42")
    assert direct.base_url == mod.GROQ_DEFAULT_BASE_URL, direct.base_url
    assert mod.GROQ_DEFAULT_BASE_URL == "https://api.groq.com/openai/v1", \
        mod.GROQ_DEFAULT_BASE_URL

    # Missing api_key -> clear config error (never silently empty / hard-coded).
    try:
        mod.GroqProvider(model="llama-3.3-70b-versatile")
    except mod.ProviderConfigError as exc:
        assert "API key" in str(exc), exc
    else:
        raise AssertionError("groq without API key should raise ProviderConfigError")
    try:
        mod.build_provider(mod.ProviderSettings(
            provider="groq", model="llama-3.3-70b-versatile"
        ))
    except mod.ProviderConfigError:
        pass
    else:
        raise AssertionError("build_provider(groq) without API key should raise")

    # The default provider remains ollama; unknown provider message lists groq.
    defaults = mod.ProviderSettings(model="llama3.1")
    assert defaults.select_provider() == "ollama", defaults.select_provider()
    try:
        mod.build_provider(mod.ProviderSettings(provider="bogus", model="m"))
    except mod.ProviderConfigError as exc:
        assert "groq" in str(exc), exc
    print("OK  Groq provider selection, default base URL, and API-key requirement")


def scenario_groq_success():
    """GroqProvider must POST an OpenAI-compatible /chat/completions body with a
    Bearer Authorization header and parse choices[0].message.content."""
    mod = load_providers()
    with FakeGroqServer(mode="ok") as server:
        provider = mod.GroqProvider(
            model="llama-3.3-70b-versatile",
            base_url="http://127.0.0.1:{0}".format(server.port),
            api_key="sk-test-42",
            timeout=5.0,
        )
        response = provider.chat(
            [mod.message("user", "make a red cube")],
            temperature=0.1,
            max_tokens=64,
        )
    assert response.provider == "groq", response
    assert response.text == "hello from fake groq", response
    assert response.model == "fake-groq", response
    assert len(FakeGroqHandler.bodies) == 1, FakeGroqHandler.bodies
    body = json.loads(FakeGroqHandler.bodies[0])
    assert body["model"] == "llama-3.3-70b-versatile", body
    assert body["stream"] is False, body
    assert body["messages"] == [{"role": "user", "content": "make a red cube"}], body
    assert body["temperature"] == 0.1, body
    assert body["max_tokens"] == 64, body
    assert FakeGroqHandler.auth_headers == ["Bearer sk-test-42"], \
        FakeGroqHandler.auth_headers
    assert FakeGroqHandler.user_agents == [mod.RBXFORGE_USER_AGENT], \
        FakeGroqHandler.user_agents
    assert mod.RBXFORGE_USER_AGENT == "RBXForge/0.1.0", mod.RBXFORGE_USER_AGENT
    print("OK  groq provider success; OpenAI-compatible body + Bearer auth + User-Agent verified")


def scenario_groq_timeout():
    mod = load_providers()
    with FakeGroqServer(mode="sleep") as server:
        provider = mod.GroqProvider(
            model="llama-3.3-70b-versatile",
            base_url="http://127.0.0.1:{0}".format(server.port),
            api_key="sk-test-42",
            timeout=0.3,
        )
        try:
            provider.chat([mod.message("user", "hi")])
        except mod.ProviderTimeoutError:
            pass
        else:
            raise AssertionError("expected ProviderTimeoutError")
    print("OK  groq provider times out -> ProviderTimeoutError")


def scenario_groq_connection_error():
    mod = load_providers()
    provider = mod.GroqProvider(
        model="llama-3.3-70b-versatile",
        base_url="http://127.0.0.1:{0}".format(free_port()),
        api_key="sk-test-42",
        timeout=0.5,
    )
    try:
        provider.chat([mod.message("user", "hi")])
    except mod.ProviderConnectionError:
        pass
    else:
        raise AssertionError("expected ProviderConnectionError")
    print("OK  groq connection refused -> ProviderConnectionError")


def scenario_groq_http_and_response_errors():
    """HTTP-level and response-level errors must surface as ProviderResponseError
    through the existing typed error hierarchy (no raw urllib errors leak)."""
    mod = load_providers()
    with FakeGroqServer(mode="error") as server:
        provider = mod.GroqProvider(
            model="llama-3.3-70b-versatile",
            base_url="http://127.0.0.1:{0}".format(server.port),
            api_key="sk-test-42",
            timeout=5.0,
        )
        try:
            provider.chat([mod.message("user", "hi")])
        except mod.ProviderResponseError:
            pass
        else:
            raise AssertionError("expected ProviderResponseError from error payload")

    with FakeGroqServer(mode="http500") as server:
        provider = mod.GroqProvider(
            model="llama-3.3-70b-versatile",
            base_url="http://127.0.0.1:{0}".format(server.port),
            api_key="sk-test-42",
            timeout=5.0,
        )
        try:
            provider.chat([mod.message("user", "hi")])
        except mod.ProviderResponseError:
            pass
        else:
            raise AssertionError("expected ProviderResponseError from HTTP 500")
    print("OK  groq error payload and HTTP 500 -> ProviderResponseError")


def scenario_groq_response_parsing():
    """The parser must reject invalid model responses (missing content, empty
    choices, non-JSON) instead of guessing."""
    mod = load_providers()
    server_modes = {
        "missing_content": "choices[0] has no message.content",
        "empty_choices": "choices is empty/absent",
        "nonjson": "non-JSON body",
    }
    for mode, label in server_modes.items():
        with FakeGroqServer(mode=mode) as server:
            provider = mod.GroqProvider(
                model="llama-3.3-70b-versatile",
                base_url="http://127.0.0.1:{0}".format(server.port),
                api_key="sk-test-42",
                timeout=5.0,
            )
            try:
                provider.chat([mod.message("user", "hi")])
            except mod.ProviderResponseError:
                pass
            else:
                raise AssertionError("expected ProviderResponseError for {0}".format(label))
    print("OK  groq response parsing rejects missing content / empty choices / non-JSON")


def scenario_groq_gptoss_native_tool_compat():
    """Regression: Groq's default `tool_choice` is "none" when no `tools` are
    sent, so a tool-capable model like GPT-OSS that calls a tool natively is
    rejected with HTTP 400 "Tool choice is none, but model called a tool".

    The fix must (a) post the real tool definitions with `tool_choice: "auto"`
    in the request, and (b) translate a native `message.tool_calls` reply back
    into the JSON-in-text the agent's parse_agent_reply expects - so the
    JSON-in-text agent architecture is preserved.
    """
    mod = load_providers()
    tool_defs = [
        {
            "name": "find_instances",
            "description": "Search the live Workspace for instances by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "min_length": 1},
                    "max_results": {"type": "number", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
            },
        },
        {
            "name": "create_part",
            "description": "Create a Part in workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": "string", "enum": ["red", "blue"]},
                    "position": {
                        "type": "object",
                        "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
                        "required": ["x", "y", "z"],
                    },
                    "size": {
                        "type": "object",
                        "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
                        "required": ["x", "y", "z"],
                    },
                },
                "required": ["name", "position", "size", "color"],
            },
        },
    ]

    with FakeGroqServer(mode="gptoss_native_tool") as server:
        provider = mod.GroqProvider(
            model="gpt-oss-120b",
            base_url="http://127.0.0.1:{0}".format(server.port),
            api_key="sk-test-42",
            timeout=5.0,
        )
        response = provider.chat(
            [mod.message("user", "find SpawnLocation and tell me about it")],
            tools=tool_defs,
        )

    # (a) Request configuration: real `tools` + `tool_choice` is "auto" (NOT
    # the default "none"), so Groq no longer rejects the native call.
    assert len(FakeGroqHandler.bodies) == 1, FakeGroqHandler.bodies
    body = json.loads(FakeGroqHandler.bodies[0])
    assert body["tool_choice"] == "auto", body
    assert [tool["type"] for tool in body["tools"]] == ["function", "function"], body
    assert [tool["function"]["name"] for tool in body["tools"]] == \
        ["find_instances", "create_part"], body["tools"]
    assert body["tools"][0]["function"]["description"] == tool_defs[0]["description"], \
        body["tools"][0]
    assert body["tools"][0]["function"]["parameters"]["required"] == ["query"], \
        body["tools"][0]
    assert body["stream"] is False, body

    # (b) The native tool call is normalized to the JSON-in-text the agent
    # parses ({"tool", "arguments"}), never left as an empty string.
    assert response.text == ('{"tool": "find_instances", '
                             '"arguments": {"query": "SpawnLocation", "max_results": 10}}'), \
        response.text

    # The translated text must be parseable by the agent layer's parser.
    agent_mod = load_agent_module()
    call = agent_mod.parse_agent_reply(response.text)
    assert call.name == "find_instances", call
    assert call.arguments == {"query": "SpawnLocation", "max_results": 10}, call

    # When a tool-capable model replies with plain content (no native call), the
    # text passes through unchanged.
    with FakeGroqServer(mode="ok") as server:
        provider = mod.GroqProvider(
            model="gpt-oss-120b",
            base_url="http://127.0.0.1:{0}".format(server.port),
            api_key="sk-test-42",
            timeout=5.0,
        )
        response = provider.chat(
            [mod.message("user", "hello")],
            tools=tool_defs,
        )
    assert response.text == "hello from fake groq", response.text
    print("OK  groq GPT-OSS: tools + tool_choice=auto sent; native calls normalized to JSON-in-text")


def scenario_mock_runtime_env():
    """build_provider must honor RBXFORGE_MOCK_RESPONSE / RBXFORGE_MOCK_FAIL so
    subprocess/REPL tests can drive the mock deterministically."""
    mod = load_providers()
    saved = {key: os.environ.get(key) for key in ("RBXFORGE_MOCK_RESPONSE", "RBXFORGE_MOCK_FAIL")}
    try:
        os.environ["RBXFORGE_MOCK_RESPONSE"] = '{"tool": "create_part", "arguments": {}}'
        os.environ.pop("RBXFORGE_MOCK_FAIL", None)
        provider = mod.build_provider(mod.ProviderSettings(provider="mock", model="mock-m"))
        response = provider.chat([mod.message("user", "make a cube")])
        assert response.text == '{"tool": "create_part", "arguments": {}}', response

        os.environ.pop("RBXFORGE_MOCK_RESPONSE", None)
        os.environ["RBXFORGE_MOCK_FAIL"] = "timeout"
        provider = mod.build_provider(mod.ProviderSettings(provider="mock", model="mock-m"))
        try:
            provider.chat([mod.message("user", "make a cube")])
        except mod.ProviderTimeoutError:
            pass
        else:
            raise AssertionError("RBXFORGE_MOCK_FAIL=timeout should make the mock time out")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("OK  build_provider mock honors RBXFORGE_MOCK_RESPONSE / RBXFORGE_MOCK_FAIL")


def scenario_cli_smoke():
    """cli/providers.py --provider mock must select the mock provider and exit 0."""
    env = dict(os.environ)
    env.update({"RBXFORGE_PROVIDER": "mock", "RBXFORGE_MODEL": "mock-model"})
    proc = subprocess.run(
        [sys.executable, PROVIDERS, "--provider", "mock"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "selected provider: mock" in proc.stdout, proc.stdout
    print("OK  cli/providers.py smoke selection works (mock)")


def main():
    scenario_mock_success()
    scenario_mock_failures()
    scenario_provider_selection()
    scenario_configuration_env()
    scenario_configuration_errors()
    scenario_ollama_success()
    scenario_ollama_timeout()
    scenario_ollama_connection_error()
    scenario_ollama_response_errors()
    scenario_groq_selection_and_config()
    scenario_groq_success()
    scenario_groq_timeout()
    scenario_groq_connection_error()
    scenario_groq_http_and_response_errors()
    scenario_groq_response_parsing()
    scenario_groq_gptoss_native_tool_compat()
    scenario_mock_runtime_env()
    scenario_cli_smoke()
    print("\nAll provider scenarios passed.")


if __name__ == "__main__":
    main()