#!/usr/bin/env python3
"""Provider-layer tests for the RBXForge AI abstraction (Phase 3A).

Covers provider selection, environment/configuration, the Ollama HTTP client
(against a fake in-process Ollama /api/chat server), timeouts/errors, and the
mock provider. Standard library only.

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


# --------------------------------------------------------------------------- #
# A fake Ollama /api/chat server
# --------------------------------------------------------------------------- #


class FakeOllamaHandler(BaseHTTPRequestHandler):
    mode = "ok"        # ok | error | http500 | sleep
    bodies = []

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
    print("OK  ollama provider success; request body verified")


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
    scenario_cli_smoke()
    print("\nAll provider scenarios passed.")


if __name__ == "__main__":
    main()