#!/usr/bin/env python3
"""Creator Store asset-discovery tests (Phase 7A).

Covers the cli/roblox_assets.py client (against a fake in-process Open Cloud
Creator Store HTTP server), its wiring into the cli/rbxforge.py tool registry /
REPL / one-shot CLI, and its exposure to the cli/agent.py agent. Standard
library only.

Run from the repository root:
    python3 tests/test_assets.py
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "cli", "roblox_assets.py")
CLI = os.path.join(ROOT, "cli", "rbxforge.py")
AGENT = os.path.join(ROOT, "cli", "agent.py")

_KEY = "test-open-cloud-key"


def load_assets():
    spec = importlib.util.spec_from_file_location("rbxforge_assets", ASSETS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cli():
    spec = importlib.util.spec_from_file_location("rbxforge_cli", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_agent():
    spec = importlib.util.spec_from_file_location("rbxforge_agent", AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assets_mod = load_assets()
cli_mod = load_cli()
agent_mod = load_agent()
providers = agent_mod.providers
rbxforge = agent_mod.rbxforge


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class RecordingProvider(providers.MockProvider):
    """MockProvider that returns scripted replies and records every chat call."""

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


def asset_call(query, asset_type=None, max_results=None):
    arguments = {"query": query}
    if asset_type is not None:
        arguments["asset_type"] = asset_type
    if max_results is not None:
        arguments["max_results"] = max_results
    return json.dumps({"tool": "asset_search", "arguments": arguments})


def recommend_call(query, asset_type=None, creator=None, max_results=None, limit=None):
    arguments = {"query": query}
    if asset_type is not None:
        arguments["asset_type"] = asset_type
    if creator is not None:
        arguments["creator"] = creator
    if max_results is not None:
        arguments["max_results"] = max_results
    if limit is not None:
        arguments["limit"] = limit
    return json.dumps({"tool": "recommend_assets", "arguments": arguments})


# --------------------------------------------------------------------------- #
# A fake Open Cloud Creator Store /toolbox-service/v2/assets:search server
# --------------------------------------------------------------------------- #


def entry(asset_id, name, asset_type="Model", creator="Roblox", creator_id="1",
          description=None, thumbnail=None, rating=None, sales=None,
          favorites=None):
    asset = {"assetId": asset_id, "name": name, "assetType": asset_type}
    if description is not None:
        asset["description"] = description
    if thumbnail is not None:
        if isinstance(thumbnail, dict):
            asset["thumbnail"] = thumbnail
        else:
            asset["thumbnailUrl"] = thumbnail
    if rating is not None:
        asset["rating"] = rating
    if sales is not None:
        asset["sales"] = sales
    if favorites is not None:
        asset["favorites"] = favorites
    creator_obj = {"displayName": creator, "id": creator_id}
    return {"asset": asset, "creator": creator_obj}


def ok_body():
    return {
        "creatorStoreAssets": [
            entry(135522, "Steel Sword", "Model", "Roblox", "1",
                  "A forged blade", "https://img.example/sword.png"),
            entry(135523, "Wooden Sword", "Model", "Roblox", "1"),
        ],
        "totalResults": 2,
    }


def recommend_body():
    return {
        "creatorStoreAssets": [
            entry(1, "Steel Sword", "Model", "Roblox", "1",
                  "A forged blade", rating=4.8, sales=320),
            entry(2, "Wooden Sword", "Model", "Roblox", "1",
                  "A blunt practice blade", rating=3.2),
            entry(3, "Enchanted Sword", "Model", "BladeWorks", "9",
                  "Shiny", rating=4.9, sales=999),
        ],
        "totalResults": 3,
    }


class FakeStoreHandler(BaseHTTPRequestHandler):
    mode = "ok"
    response_body = None
    paths = []
    api_keys = []
    user_agents = []
    bodies = []
    total_calls = 0

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

    def _send_raw(self, code, text):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self._read_body()
        self.__class__.paths.append(self.path)
        self.__class__.api_keys.append(self.headers.get("x-api-key"))
        self.__class__.user_agents.append(self.headers.get("User-Agent"))
        self.__class__.total_calls += 1
        try:
            self.__class__.bodies.append(json.loads(raw))
        except ValueError:
            self.__class__.bodies.append(raw.decode("utf-8", "replace"))

        if self.path != "/toolbox-service/v2/assets:search":
            self._send(404, {"error": "unexpected path: " + self.path})
        elif self.mode == "error":
            self._send(500, {"error": "server exploded"})
        elif self.mode == "ratelimit":
            self._send(429, {"message": "rate limited"})
        elif self.mode == "ratelimit-then-ok":
            if self.total_calls == 1:
                self._send(429, {"message": "rate limited"})
            else:
                self._send(200, self.response_body or ok_body())
        elif self.mode == "malformed-json":
            self._send_raw(200, "this is not json at all")
        elif self.mode == "sleep":
            time.sleep(2.0)
            self._send(200, self.response_body or ok_body())
        else:
            self._send(200, self.response_body or ok_body())

    def log_message(self, format, *args):
        pass


class FakeStoreServer:
    def __init__(self, mode="ok", body=None):
        FakeStoreHandler.mode = mode
        FakeStoreHandler.response_body = body
        FakeStoreHandler.paths = []
        FakeStoreHandler.api_keys = []
        FakeStoreHandler.user_agents = []
        FakeStoreHandler.bodies = []
        FakeStoreHandler.total_calls = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeStoreHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def paths(self):
        return FakeStoreHandler.paths

    @property
    def bodies(self):
        return FakeStoreHandler.bodies

    @property
    def api_keys(self):
        return FakeStoreHandler.api_keys

    @property
    def user_agents(self):
        return FakeStoreHandler.user_agents

    @property
    def total_calls(self):
        return FakeStoreHandler.total_calls


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------- #
# Env helpers used by the CLI subprocess scenarios
# --------------------------------------------------------------------------- #


def env_with_server(port, **extra):
    env = dict(os.environ)
    env.pop("RBXFORGE_OPEN_CLOUD_API_KEY", None)
    env.pop("ROBLOX_OPEN_CLOUD_API_KEY", None)
    env.update({
        "RBXFORGE_OPEN_CLOUD_API_KEY": _KEY,
        "RBXFORGE_OPEN_CLOUD_BASE_URL": "http://127.0.0.1:{0}".format(port),
    })
    env.update(extra)
    return env


def full_server_url(port):
    return "http://127.0.0.1:{0}".format(port)


# --------------------------------------------------------------------------- #
# Client configuration
# --------------------------------------------------------------------------- #


def scenario_config():
    mod = assets_mod
    # No key at all -> AssetConfigError naming the env var.
    empty = {"RBXFORGE_OPEN_CLOUD_API_KEY": "", "ROBLOX_OPEN_CLOUD_API_KEY": None}
    try:
        mod.asset_client_from_env(env=empty)
        raise AssertionError("expected AssetConfigError for missing API key")
    except mod.AssetConfigError as exc:
        assert "RBXFORGE_OPEN_CLOUD_API_KEY" in str(exc), exc

    # Primary key is used.
    client = mod.asset_client_from_env(env={"RBXFORGE_OPEN_CLOUD_API_KEY": _KEY})
    assert client.api_key == _KEY
    assert client.base_url == mod.DEFAULT_BASE_URL
    assert client.timeout == mod.DEFAULT_TIMEOUT

    # Fallback key is used when the primary is absent.
    client = mod.asset_client_from_env(
        env={"ROBLOX_OPEN_CLOUD_API_KEY": "fallback-key"}
    )
    assert client.api_key == "fallback-key"

    # base_url is stripped of a trailing slash.
    client = mod.asset_client_from_env(env={
        "RBXFORGE_OPEN_CLOUD_API_KEY": _KEY,
        "RBXFORGE_OPEN_CLOUD_BASE_URL": "http://localhost:9000/",
    })
    assert client.base_url == "http://localhost:9000"

    # Timeout override parsed; invalid values rejected.
    client = mod.asset_client_from_env(env={
        "RBXFORGE_OPEN_CLOUD_API_KEY": _KEY,
        "RBXFORGE_OPEN_CLOUD_TIMEOUT": "1.5",
    })
    assert client.timeout == 1.5
    for bad in ("abc", "0", "-2"):
        try:
            mod.asset_client_from_env(env={
                "RBXFORGE_OPEN_CLOUD_API_KEY": _KEY,
                "RBXFORGE_OPEN_CLOUD_TIMEOUT": bad,
            })
            raise AssertionError("expected AssetConfigError for timeout {0!r}".format(bad))
        except mod.AssetConfigError:
            pass

    # Empty api_key is rejected at construction.
    try:
        mod.RobloxAssetClient(api_key="")
        raise AssertionError("expected AssetConfigError for empty api_key")
    except mod.AssetConfigError:
        pass
    print("OK  asset client environment configuration (key/env/fallback/base_url/timeout)")


def scenario_client_validation_and_clamps():
    mod = assets_mod
    client = mod.RobloxAssetClient(_KEY, base_url=full_server_url(free_port()))
    for bad in ("", "   ", 0):
        try:
            client.search(bad)
            raise AssertionError("expected AssetConfigError for query {0!r}".format(bad))
        except mod.AssetConfigError:
            pass
    try:
        client.search("x" * (mod.MAX_QUERY_LENGTH + 1))
        raise AssertionError("expected AssetConfigError for over-long query")
    except mod.AssetConfigError:
        pass
    try:
        client.search("sword", asset_type="NotARealType")
        raise AssertionError("expected AssetConfigError for unknown asset_type")
    except mod.AssetConfigError:
        pass

    assert mod.ASSET_TYPES == [
        "Audio", "Model", "Decal", "Plugin", "MeshPart", "Video", "FontFamily",
    ], mod.ASSET_TYPES
    # Valid asset_types pass validation; the search() method raises no error
    # before attempting the HTTP call. We verify this by intercepting _post_json.
    results = []
    original_post = client._post_json

    def intercept_post(body, timeout=None):
        results.append(body)
        return {"creatorStoreAssets": []}

    client._post_json = intercept_post
    for valid in mod.ASSET_TYPES:
        client.search("sword", asset_type=valid)
        assert results[-1].get("searchCategoryType") == valid
    client._post_json = original_post
    print("OK  client argument validation (query/asset_type allowlist/length)")


# --------------------------------------------------------------------------- #
# Client HTTP + parsing
# --------------------------------------------------------------------------- #


def client_for(port, **kwargs):
    return assets_mod.RobloxAssetClient(
        _KEY, base_url=full_server_url(port), **kwargs
    )


def scenario_search_success():
    mod = assets_mod
    with FakeStoreServer() as server:
        client = client_for(server.port)
        result = client.search("  sword  ")
        assert len(server.paths) == 1
        assert server.paths[0] == "/toolbox-service/v2/assets:search"
        assert server.api_keys == [_KEY]
        assert server.user_agents == [mod.ASSET_SEARCH_USER_AGENT]
        body = server.bodies[0]
        assert body == {"query": "sword", "maxPageSize": mod.DEFAULT_MAX_RESULTS}, body

        assert result["query"] == "sword"
        assert result["asset_type"] is None
        assert result["max_results"] == mod.DEFAULT_MAX_RESULTS
        assert result["count"] == 2
        assert result["total"] == 2
        assert result["truncated"] is False
        assert result["results"][0] == {
            "asset_id": "135522",
            "name": "Steel Sword",
            "asset_type": "Model",
            "creator": "Roblox",
            "creator_id": "1",
            "description": "A forged blade",
            "thumbnail_url": "https://img.example/sword.png",
        }, result["results"][0]
        assert result["results"][1] == {
            "asset_id": "135523",
            "name": "Wooden Sword",
            "asset_type": "Model",
            "creator": "Roblox",
            "creator_id": "1",
        }, result["results"][1]
    print("OK  Creator Store search request (path/headers/body) + parsed results")


def scenario_search_truncated_and_clamp():
    with FakeStoreServer(body={
        "creatorStoreAssets": [
            entry(1, "A"), entry(2, "B"), entry(3, "C"),
        ],
        "totalResults": 25,
    }) as server:
        client = client_for(server.port)
        result = client.search("sword", max_results=20)
        assert result["count"] == 3 and result["total"] == 25
        assert result["truncated"] is True
        assert server.bodies[0]["maxPageSize"] == 20
        assert result["max_results"] == 20

        # Out-of-range max_results is clamped to [1, MAX_RESULTS].
        client2 = client_for(server.port)
        low = client2.search("sword", max_results=0)
        assert low["max_results"] == 1 and server.bodies[1]["maxPageSize"] == 1
        high = client2.search("sword", max_results=99)
        assert high["max_results"] == assets_mod.MAX_RESULTS and server.bodies[2]["maxPageSize"] == assets_mod.MAX_RESULTS
    print("OK  truncation flag (nextPageToken-like totalResults > count) + max_results clamp")


def scenario_search_asset_type_filter():
    for asset_type in ("Model", "Plugin"):
        with FakeStoreServer() as server:
            client = client_for(server.port)
            result = client.search("sword", asset_type=asset_type)
            assert server.bodies[0]["searchCategoryType"] == asset_type
            assert result["asset_type"] == asset_type
    print("OK  searchCategoryType filter sent + echoed in the result")


def scenario_search_defensive_parsing():
    with FakeStoreServer(body={
        "creatorStoreAssets": [
            {"asset": "not-an-object"},                    # skipped
            "junk",                                        # skipped
            {"asset": {"assetId": 999, "name": "OnlyId",
                       "assetType": "Decal"}},             # no creator
            {"asset": {"assetId": 1000},
             "creator": {"id": "creator-only"}},           # creator id fallback name
            {"asset": {"assetId": 1001, "name": "Thumb",
                       "thumbnail": {"url": "https://t/1.png"},
                       "assetType": "MeshPart"}},          # nested thumbnail
            {"asset": {"assetId": 1002, "name": "Descless",
                       "description": "", "assetType": "Audio"}},  # empty desc omitted
        ],
        "totalResults": 99,
    }) as server:
        client = client_for(server.port)
        result = client.search("sword")
        assert result["count"] == 4, result["results"]
        results = {r["asset_id"]: r for r in result["results"]}
        assert results["999"] == {"asset_id": "999", "name": "OnlyId", "asset_type": "Decal"}
        assert results["1000"] == {"asset_id": "1000", "creator": "creator-only",
                                   "creator_id": "creator-only"}
        assert results["1001"]["thumbnail_url"] == "https://t/1.png"
        assert "description" not in results["1002"]
    print("OK  defensive parsing tolerates janky/partial entries; skips unusable ones")


def scenario_search_rating_usage_metadata():
    """Phase 7B: the parser captures optional rating/usage metadata only when
    the API provides it (backward-compatible with the Phase 7A shape)."""
    with FakeStoreServer(body={
        "creatorStoreAssets": [
            # Primary keys -> rating / sales / favorites.
            entry(1, "A", rating=4.7, sales=100, favorites=25),
            # Alternate API keys -> averageRating / totalSales.
            {"asset": {"assetId": 2, "name": "B", "assetType": "Model",
                       "averageRating": 3.5, "totalSales": 10}},
            # rating is "bogus" (a string) -> rejected; favoriteCount still parses.
            {"asset": {"assetId": 3, "name": "C", "assetType": "Model",
                       "rating": "bogus", "favoriteCount": 5}},
            # No usage metadata at all -> nothing added (the Phase 7A shape).
            entry(4, "D"),
        ],
        "totalResults": 4,
    }) as server:
        client = client_for(server.port)
        result = client.search("sword")
        entries = {r["asset_id"]: r for r in result["results"]}
        assert entries["1"]["rating"] == 4.7
        assert entries["1"]["sales_count"] == 100
        assert entries["1"]["favorite_count"] == 25
        assert entries["2"]["rating"] == 3.5
        assert entries["2"]["sales_count"] == 10
        assert "favorite_count" not in entries["2"]
        assert "rating" not in entries["3"] and "sales_count" not in entries["3"]
        assert entries["3"]["favorite_count"] == 5
        assert set(entries["4"]) == {
            "asset_id", "name", "asset_type", "creator", "creator_id",
        }, entries["4"]  # Phase 7A exact shape preserved when no usage present
    print("OK  parser captures rating/sales/favorites (incl. alternate keys); "
          "missing metadata absent; janky values rejected; old shape preserved")


def scenario_search_response_shapes():
    mod = assets_mod
    # Response with no creatorStoreAssets field -> empty result list.
    with FakeStoreServer(body={"totalResults": 5}) as server:
        result = client_for(server.port).search("sword")
        assert result["results"] == [] and result["count"] == 0
        assert result["total"] == 5  # raw totalResults preserved when no entries

    # totalResults as a non-int -> falls back to count.
    with FakeStoreServer(body={"creatorStoreAssets": [entry(1, "A")],
                               "totalResults": "25"}) as server:
        result = client_for(server.port).search("sword")
        assert result["total"] == 1 and result["truncated"] is False

    # creatorStoreAssets present but not a list -> AssetResponseError.
    with FakeStoreServer(body={"creatorStoreAssets": {"asset": {}}}) as server:
        try:
            client_for(server.port).search("sword")
            raise AssertionError("expected AssetResponseError")
        except mod.AssetResponseError as exc:
            assert "not a list" in str(exc), exc

    # Non-object JSON response -> AssetResponseError.
    with FakeStoreServer(body=[1, 2, 3]) as server:
        try:
            client_for(server.port).search("sword")
            raise AssertionError("expected AssetResponseError")
        except mod.AssetResponseError:
            pass

    # Non-JSON body -> AssetResponseError.
    with FakeStoreServer(mode="malformed-json") as server:
        try:
            client_for(server.port).search("sword")
            raise AssertionError("expected AssetResponseError")
        except mod.AssetResponseError as exc:
            assert "non-JSON" in str(exc), exc

    # HTTP 500 -> AssetResponseError with the status.
    with FakeStoreServer(mode="error") as server:
        try:
            client_for(server.port).search("sword")
            raise AssertionError("expected AssetResponseError")
        except mod.AssetResponseError as exc:
            assert "HTTP 500" in str(exc), exc
    print("OK  response shape errors (missing/non-list/non-object/non-JSON/HTTP 500)")


def scenario_search_rate_limit():
    mod = assets_mod
    # 429 once, then success: retried transparently.
    with FakeStoreServer(mode="ratelimit-then-ok") as server:
        client = client_for(server.port, max_attempts=3, retry_backoff_base=0.01)
        result = client.search("sword")
        assert result["count"] == 2
        assert server.total_calls == 2

    # Persistent 429: AssetRateLimitError after the retry budget.
    with FakeStoreServer(mode="ratelimit") as server:
        client = client_for(server.port, max_attempts=2, retry_backoff_base=0.01)
        try:
            client.search("sword")
            raise AssertionError("expected AssetRateLimitError")
        except mod.AssetRateLimitError as exc:
            assert "429" in str(exc), exc
        assert server.total_calls == 2
    print("OK  rate limiting: 429 retried with backoff; budget exhaustion raises")


def scenario_search_connection_and_timeout():
    mod = assets_mod
    # Connection refused -> AssetConnectionError.
    client = mod.RobloxAssetClient(_KEY, base_url="http://127.0.0.1:{0}".format(free_port()))
    try:
        client.search("sword")
        raise AssertionError("expected AssetConnectionError")
    except mod.AssetConnectionError:
        pass

    # Server too slow -> AssetTimeoutError (a subclass of AssetConnectionError).
    with FakeStoreServer(mode="sleep") as server:
        client = client_for(server.port, timeout=0.15)
        try:
            client.search("sword")
            raise AssertionError("expected AssetTimeoutError")
        except mod.AssetTimeoutError:
            pass
    print("OK  connection refused -> AssetConnectionError; slow server -> AssetTimeoutError")


def scenario_search_per_call_timeout():
    mod = assets_mod
    # A per-call timeout override bounds a single request even when the client
    # was configured with a large timeout.
    with FakeStoreServer(mode="sleep") as server:
        client = client_for(server.port, timeout=10.0)
        try:
            client.search("sword", timeout=0.15)
            raise AssertionError("expected AssetTimeoutError")
        except mod.AssetTimeoutError:
            pass

    # A non-positive per-call timeout is rejected as a config error.
    client = client_for(free_port())
    for bad in (0, -1):
        try:
            client.search("sword", timeout=bad)
            raise AssertionError("expected AssetConfigError for timeout {0!r}".format(bad))
        except mod.AssetConfigError:
            pass
    print("OK  per-call timeout override bounds the request; non-positive values rejected")


# --------------------------------------------------------------------------- #
# Registry + tool layer (cli/rbxforge.py)
# --------------------------------------------------------------------------- #


def scenario_registry_exposes_asset_search():
    mod = cli_mod
    registry = mod.default_registry()
    names = [tool.name for tool in registry.list()]
    assert "asset_search" in names, names
    tool = registry.get("asset_search")
    assert isinstance(tool.description, str) and tool.description
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["query"], schema
    assert set(schema["properties"]) == {"query", "asset_type", "max_results"}, schema
    assert schema["properties"]["asset_type"]["enum"] == assets_mod.ASSET_TYPES
    assert schema["properties"]["max_results"] == {
        "type": "number", "integer": True, "minimum": 1, "maximum": mod.MAX_ASSET_RESULTS,
    }, schema["properties"]["max_results"]
    assert "asset_type" in schema["properties"] and "query" in schema["required"]
    print("OK  default registry exposes asset_search with schema (enum/queries/max 20)")


def scenario_registry_exposes_recommend():
    mod = cli_mod
    registry = mod.default_registry()
    names = [tool.name for tool in registry.list()]
    assert "recommend_assets" in names, names
    tool = registry.get("recommend_assets")
    assert isinstance(tool.description, str) and tool.description
    assert "read-only" in tool.description, tool.description
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["query"], schema
    assert set(schema["properties"]) == {
        "query", "asset_type", "creator", "max_results", "limit",
    }, schema
    assert schema["properties"]["asset_type"]["enum"] == assets_mod.ASSET_TYPES
    assert schema["properties"]["max_results"]["maximum"] == mod.MAX_ASSET_RESULTS
    assert schema["properties"]["limit"] == {
        "type": "number", "integer": True, "minimum": 1,
        "maximum": mod.MAX_ASSET_RECOMMENDATIONS,
    }, schema["properties"]["limit"]
    print("OK  default registry exposes recommend_assets with schema "
          "(query/type/creator/max_results/limit 1..5)")


def scenario_tool_execution_recommend():
    mod = cli_mod
    with FakeStoreServer(body=recommend_body()) as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            result = rbx.execute_tool("recommend_assets", {"query": "sword"})
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert result is not False and isinstance(result, dict)
        assert result["query"] == "sword" and result["evaluated"] == 3
        assert result["count"] == 3  # 3 results, default limit 3
        assert [r["asset"]["name"] for r in result["recommendations"]] == [
            "Enchanted Sword", "Steel Sword", "Wooden Sword",
        ], result["recommendations"]
        # Enchanted (5) and Steel (5) tie at the top: deterministic name break.
        assert [r["score"] for r in result["recommendations"]] == [5, 5, 3]
        assert "usage metadata" in result["recommendations"][0]["reason"]
        # Exactly one search was made: ranking performs NO extra API calls.
        assert server.total_calls == 1, server.total_calls
        assert any("recommend_assets OK: ranked 3 of 3 result(s) for query 'sword'" in line
                   for line in logs), logs
    print("OK  recommend_assets tool ranks search metadata deterministically; "
          "one API call, no plugin traffic")


def scenario_tool_execution_recommend_params():
    mod = cli_mod
    with FakeStoreServer(body=recommend_body()) as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            # asset_type + max_results flow into the HTTP request; creator and
            # limit flow into ranking.
            result = rbx.execute_tool("recommend_assets", {
                "query": "sword", "asset_type": "Model", "creator": "BladeWorks",
                "max_results": 3, "limit": 1,
            })
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert server.bodies[-1] == {
            "query": "sword", "maxPageSize": 3, "searchCategoryType": "Model",
        }, server.bodies[-1]
        assert result["creator"] == "BladeWorks"
        assert result["limit"] == 1 and result["count"] == 1
        top = result["recommendations"][0]
        assert top["asset"]["name"] == "Enchanted Sword", result
        assert "BladeWorks" in top["reason"], top["reason"]
    print("OK  recommend_assets forwards type/max_results to the search and "
          "creator/limit into ranking")


def scenario_tool_execution_recommend_failures():
    mod = cli_mod

    # Missing API key: False + FAILED log (same handling as asset_search).
    os.environ.pop("RBXFORGE_OPEN_CLOUD_API_KEY", None)
    os.environ.pop("ROBLOX_OPEN_CLOUD_API_KEY", None)
    os.environ.pop("RBXFORGE_OPEN_CLOUD_BASE_URL", None)
    logs = []
    rbx = mod.RBXForge(console=_CapturingConsole(logs))
    result = rbx.execute_tool("recommend_assets", {"query": "sword"})
    assert result is False
    assert any("recommend_assets FAILED" in line and "API key" in line for line in logs), logs

    # HTTP failure through the env client: False + FAILED log.
    with FakeStoreServer(mode="error") as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            result = rbx.execute_tool("recommend_assets", {"query": "sword"})
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert result is False
        assert any("recommend_assets FAILED" in line and "HTTP 500" in line for line in logs), logs

    # The tool forwards its request timeout to the client, so a slow API is
    # bounded by the tool timeout instead of the env-configured default (30s).
    with FakeStoreServer(mode="sleep") as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            result = rbx.execute_tool("recommend_assets", {"query": "sword"}, timeout=0.15)
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert result is False
        assert any("recommend_assets FAILED" in line and "timed out" in line for line in logs), logs

    # Schema-invalid params are rejected before any HTTP call: empty query,
    # out-of-range limit/max_results, unknown asset_type, empty creator.
    with FakeStoreServer() as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            for bad in ({"query": ""}, {"query": "sword", "limit": 0},
                        {"query": "sword", "limit": 6},
                        {"query": "sword", "max_results": 21},
                        {"query": "sword", "asset_type": "Bogus"},
                        {"query": "sword", "creator": ""}):
                result = rbx.execute_tool("recommend_assets", bad)
                assert result is False, (bad, result)
                assert any("invalid parameters" in line for line in logs), (bad, logs)
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert server.total_calls == 0  # nothing reached the fake API
    print("OK  recommend_assets failures (missing key / HTTP error / timeout / "
          "schema reject incl. limit bounds)")


def scenario_tool_execution_injected_client():
    mod = cli_mod
    with FakeStoreServer() as server:
        logs = []
        rbx = mod.RBXForge(console=_CapturingConsole(logs))
        # Use env vars so the tool builds its own client and exception types
        # come from the same module object it validates against.
        environ = {
            "RBXFORGE_OPEN_CLOUD_API_KEY": _KEY,
            "RBXFORGE_OPEN_CLOUD_BASE_URL": full_server_url(server.port),
        }
        os.environ.update(environ)
        try:
            result = rbx.execute_tool("asset_search", {"query": "sword"})

            # asset_type + max_results pass through the tool into the HTTP request.
            rbx.execute_tool("asset_search", {"query": "sword", "asset_type": "Model",
                                              "max_results": 3})
        finally:
            for key in environ:
                os.environ.pop(key, None)
        assert result is not False and isinstance(result, dict)
        assert result["count"] == 2 and result["truncated"] is False
        assert any("asset_search OK: 2 result(s) for query 'sword'" in line for line in logs), logs
        assert server.bodies[-1]["searchCategoryType"] == "Model"
        assert server.bodies[-1]["maxPageSize"] == 3
    print("OK  asset_search tool executes (env client); params forwarded (incl. type)")


def scenario_tool_execution_failures():
    mod = cli_mod

    # No API key configured: tool returns False and logs the config failure.
    os.environ.pop("RBXFORGE_OPEN_CLOUD_API_KEY", None)
    os.environ.pop("ROBLOX_OPEN_CLOUD_API_KEY", None)
    os.environ.pop("RBXFORGE_OPEN_CLOUD_BASE_URL", None)
    logs = []
    rbx = mod.RBXForge(console=_CapturingConsole(logs))
    result = rbx.execute_tool("asset_search", {"query": "sword"})
    assert result is False
    assert any("asset_search FAILED" in line and "API key" in line for line in logs), logs

    # HTTP failure through the env client: False + FAILED log.
    with FakeStoreServer(mode="error") as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            result = rbx.execute_tool("asset_search", {"query": "sword"})
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert result is False
        assert any("asset_search FAILED" in line and "HTTP 500" in line for line in logs), logs

    # The tool forwards its request timeout to the client, so a slow API is
    # bounded by the tool timeout instead of the env-configured default (30s).
    with FakeStoreServer(mode="sleep") as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            result = rbx.execute_tool("asset_search", {"query": "sword"}, timeout=0.15)
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert result is False
        assert any("asset_search FAILED" in line and "timed out" in line for line in logs), logs

    # Schema-invalid params are rejected before any HTTP call.
    with FakeStoreServer() as server:
        os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"] = _KEY
        os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"] = full_server_url(server.port)
        try:
            logs = []
            rbx = mod.RBXForge(console=_CapturingConsole(logs))
            for bad in ({"query": ""}, {"query": "sword", "max_results": 21},
                        {"query": "sword", "max_results": 0},
                        {"query": "sword", "asset_type": "Bogus"}):
                result = rbx.execute_tool("asset_search", bad)
                assert result is False, (bad, result)
                assert any("invalid parameters" in line for line in logs), (bad, logs)
        finally:
            del os.environ["RBXFORGE_OPEN_CLOUD_API_KEY"]
            del os.environ["RBXFORGE_OPEN_CLOUD_BASE_URL"]
        assert server.total_calls == 0  # nothing reached the fake API
    print("OK  asset_search tool failures (missing key / HTTP error / timeout / schema reject)")


# --------------------------------------------------------------------------- #
# Agent exposure (cli/agent.py)
# --------------------------------------------------------------------------- #


def scenario_agent_exposure():
    mod = agent_mod
    registry = rbxforge.default_registry()
    defs = mod.tool_definitions(registry)
    names = [entry["name"] for entry in defs]
    assert "asset_search" in names, names
    as_def = [entry for entry in defs if entry["name"] == "asset_search"][0]
    assert set(as_def["parameters"]["required"]) == {"query"}

    # asset_search is NOT an action tool: it must never end the agent loop.
    assert "asset_search" not in mod.ACTION_TOOLS, mod.ACTION_TOOLS

    # The system prompt tells the model about it (read-only).
    prompt = mod.build_system_prompt(registry)
    assert "asset_search" in prompt and "read-only" in prompt

    # compact_tool_result renders a local (no send_request) dict result as
    # bounded JSON instead of a repr.
    call = mod.ToolCall("asset_search", {"query": "sword"})
    output = {
        "query": "sword", "asset_type": None, "max_results": 5, "count": 1,
        "total": 1, "truncated": False,
        "results": [{
            "asset_id": "135522", "name": "Steel Sword", "asset_type": "Model",
            "creator": "Roblox", "creator_id": "1",
            "description": "A forged blade", "thumbnail_url": "https://img.example/s.png",
        }],
    }
    text = mod.compact_tool_result(call, output, None)
    assert len(text) <= mod.MAX_TOOL_RESULT_CHARS
    assert json.loads(text)["results"][0]["name"] == "Steel Sword"
    # The response-payload branch is unchanged.
    payload = {"ok": True, "result": {"name": "Steel Sword"}}
    payload_text = mod.compact_tool_result(call, output, payload)
    assert "result" in payload_text and "Steel Sword" in payload_text
    print("OK  agent exposes asset_search (definitions/prompt); not action; compacted JSON result")


def scenario_asset_search_does_not_end_loop():
    mod = agent_mod
    with FakeStoreServer() as server:
        client = assets_mod.RobloxAssetClient(_KEY, base_url=full_server_url(server.port))

        class FakeRBX:
            def __init__(self, client):
                self.client = client
                self.requests = []
                self.logs = []

            def send_request(self, tool, params, timeout):
                self.requests.append((tool, params))
                return {"ok": True, "result": {"anything": True}}

            def log(self, message):
                self.logs.append(message)

            def assets(self):
                return self.client

        provider = RecordingProvider([
            asset_call("sword"),
            json.dumps({"message": "found some swords"}),
        ])
        rbx = FakeRBX(client)
        registry = rbxforge.default_registry()
        result = mod.Agent(provider, registry=registry, rbx=rbx).run("find a sword asset")

        assert result.ok is True, result
        assert [step["tool"] for step in result.steps] == ["asset_search"], result.steps
        assert result.steps[0]["ok"] is True, result.steps
        # The loop continued past the successful asset_search to the final
        # report -- two chat calls happened, proving asset_search did not end it.
        assert len(provider.chat_calls) == 2, len(provider.chat_calls)
        # asset_search never touched the plugin/WebSocket path.
        assert rbx.requests == [], rbx.requests
    print("OK  asset_search mid-loop does not end the loop; no plugin request sent")


def scenario_agent_recommend_flow():
    """Agent-level Phase 7B flow: asset_search -> ranking -> recommendation.

    The model first searches, then asks for a recommendation; recommend_assets
    collects the ranked result and feeds it back, so the next step can report.
    Neither asset_search nor recommend_assets ends the loop, and neither sends
    a plugin request."""
    mod = agent_mod
    with FakeStoreServer(body=recommend_body()) as server:
        client = assets_mod.RobloxAssetClient(_KEY, base_url=full_server_url(server.port))

        class FakeRBX:
            def __init__(self, client):
                self.client = client
                self.requests = []
                self.logs = []

            def send_request(self, tool, params, timeout):
                self.requests.append((tool, params))
                return {"ok": True, "result": {"anything": True}}

            def log(self, message):
                self.logs.append(message)

            def assets(self):
                return self.client

        provider = RecordingProvider([
            asset_call("sword"),
            recommend_call("sword", limit=2),
            json.dumps({"message": "recommend the Enchanted Sword (4.9/5)"}),
        ])
        rbx = FakeRBX(client)
        registry = rbxforge.default_registry()
        result = mod.Agent(provider, registry=registry, rbx=rbx).run("find a sword and recommend one")

        assert result.ok is True, result
        assert [step["tool"] for step in result.steps] == [
            "asset_search", "recommend_assets",
        ], result.steps
        assert all(step["ok"] for step in result.steps), result.steps
        assert len(provider.chat_calls) == 3, provider.chat_calls
        # The recommend step returned a bounded ranked result to the next turn.
        step = result.steps[1]
        assert step["output"]["recommendations"][0]["asset"]["name"] == "Enchanted Sword", step
        assert step["output"]["count"] == 2, step  # limit=2 respected
        # ..and it was compacted into the bounded text shown to the model.
        assert json.loads(step["result"])["count"] == 2, step["result"]
        # Neither local tool touched the plugin/WebSocket path.
        assert rbx.requests == [], rbx.requests
    print("OK  agent flow asset_search -> ranking -> recommendation; loop continues; "
          "no plugin request sent")


def scenario_agent_insert_flow():
    """Agent-level Phase 7C flow: search -> rank -> select -> insert -> verify.

    The model searches the store, ranks the results, picks the ranked asset,
    inserts it exactly by the id the search returned (never invented), and
    verifies the placed instance once. Only insert_asset and inspect_instance
    touch the plugin/WebSocket path; the id is enforced against the ids the
    search/ranking recorded in this session."""
    mod = agent_mod
    with FakeStoreServer(body=recommend_body()) as server:
        client = assets_mod.RobloxAssetClient(_KEY, base_url=full_server_url(server.port))

        class FakeRBX:
            def __init__(self, client):
                self.client = client
                self.requests = []
                self.logs = []
                self.known = {}

            def send_request(self, tool, params, timeout):
                self.requests.append((tool, params))
                if tool == "insert_asset":
                    return {
                        "ok": True,
                        "result": {
                            "asset_id": params["asset_id"],
                            "name": "Enchanted Sword",
                            "class": "Model",
                            "parent_path": "Workspace",
                            "path": "Workspace/Enchanted Sword",
                            "positioned": True,
                            "placement": "default",
                            "position": {"x": 5, "y": 0, "z": 0},
                        },
                    }
                if tool == "inspect_instance":
                    return {
                        "ok": True,
                        "result": {
                            "name": "Enchanted Sword", "className": "Model",
                            "path": "Workspace/Enchanted Sword", "properties": {},
                        },
                    }
                return {"ok": True, "result": {"anything": True}}

            def log(self, message):
                self.logs.append(message)

            def assets(self):
                return self.client

            def remember_assets(self, results):
                for entry in results or []:
                    if isinstance(entry, dict) and entry.get("asset_id") is not None:
                        self.known[str(entry["asset_id"])] = entry

            def asset_known(self, asset_id):
                return str(asset_id) in self.known

            def known_asset(self, asset_id):
                return self.known.get(str(asset_id))

        provider = RecordingProvider([
            asset_call("sword"),
            recommend_call("sword", limit=2),
            json.dumps({"tool": "insert_asset", "arguments": {"asset_id": "3"}}),
            json.dumps({
                "tool": "inspect_instance",
                "arguments": {"path": "Workspace/Enchanted Sword"},
            }),
        ])
        rbx = FakeRBX(client)
        registry = rbxforge.default_registry()
        result = mod.Agent(provider, registry=registry, rbx=rbx).run(
            "find a good sword, pick the best, and put it in the project"
        )

        assert result.ok is True, result
        assert result.tool.name == "insert_asset", result
        assert [step["tool"] for step in result.steps] == [
            "asset_search", "recommend_assets", "insert_asset", "inspect_instance",
        ], result.steps
        assert all(step["ok"] for step in result.steps), result.steps
        # Loop ended after the single verification step - exactly four chats.
        assert len(provider.chat_calls) == 4, provider.chat_calls
        # The id inserted came exactly from the search/ranking results.
        assert rbx.requests[0] == ("insert_asset", {"asset_id": "3"}), rbx.requests
        assert rbx.requests[1] == (
            "inspect_instance", {"path": "Workspace/Enchanted Sword"},
        ), rbx.requests
        assert len(rbx.requests) == 2, rbx.requests
        # asset_search + recommend_assets ran locally only (no plugin request).
        assert any("asset_search OK" in line for line in rbx.logs), rbx.logs
        assert any("insert_asset OK" in line for line in rbx.logs), rbx.logs
    print("OK  agent flow search -> rank -> select -> insert -> verify; only the "
          "insert/verify steps touch the plugin")


# --------------------------------------------------------------------------- #
# CLI one-shot + REPL (subprocess, against the in-process fake server)
# --------------------------------------------------------------------------- #


def run_cli(args, env):
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def scenario_cli_one_shot_success():
    with FakeStoreServer() as server:
        env = env_with_server(server.port)
        proc = run_cli(["--asset-search-once", "--query", "sword"], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "asset_search OK: 2 result(s) for query 'sword'" in proc.stdout, proc.stdout
        assert "tools registered: asset_search" in proc.stdout, proc.stdout
        assert server.bodies[0] == {"query": "sword", "maxPageSize": 5}, server.bodies

        # asset_type + max-results flow through to the request body.
        proc = run_cli(["--asset-search-once", "--query", "sword",
                        "--asset-type", "Model", "--max-results", "3"], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert server.bodies[-1] == {
            "query": "sword", "maxPageSize": 3, "searchCategoryType": "Model",
        }, server.bodies[-1]

        # --asset-search-once is a local HTTP call: no WebSocket server, no
        # "load the plugin" / "listening on ws://" guidance.
        assert "listening on ws://" not in proc.stdout, proc.stdout
        assert "load the RBXForge plugin" not in proc.stdout, proc.stdout
    print("OK  --asset-search-once success (exit 0, log line, body); type+max-results flags; no server start")


def scenario_cli_one_shot_invalid_asset_type():
    # --asset-type is an argparse choice now: an unknown value is a usage
    # error (exit 2), not a runtime execution failure (exit 4), and nothing
    # reaches the API.
    with FakeStoreServer() as server:
        proc = run_cli(["--asset-search-once", "--query", "sword",
                        "--asset-type", "Bogus"], env_with_server(server.port))
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "invalid choice" in combined, combined
        assert server.total_calls == 0
    print("OK  --asset-search-once --asset-type Bogus is an argparse usage error (exit 2)")


def scenario_cli_one_shot_errors():
    # Missing query -> usage error, exit 2, no HTTP call.
    with FakeStoreServer() as server:
        proc = run_cli(["--asset-search-once"], env_with_server(server.port))
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "--asset-search-once requires --query" in combined, combined
        assert server.total_calls == 0

    # Server error -> failure, exit 4.
    with FakeStoreServer(mode="error") as server:
        proc = run_cli(["--asset-search-once", "--query", "sword"],
                       env_with_server(server.port))
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 4, combined
        assert "asset_search FAILED" in combined, combined

    # No API key configured -> failure, exit 4.
    env = dict(os.environ)
    env.pop("RBXFORGE_OPEN_CLOUD_API_KEY", None)
    env.pop("ROBLOX_OPEN_CLOUD_API_KEY", None)
    proc = run_cli(["--asset-search-once", "--query", "sword"], env)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 4, combined
    assert "asset_search FAILED" in combined and "API key" in combined, combined
    print("OK  --asset-search-once errors (missing --query exit 2; server failure / no key exit 4)")


def scenario_cli_repl_asset_search():
    with FakeStoreServer() as server:
        env = env_with_server(server.port)
        child = subprocess.Popen(
            [sys.executable, CLI],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        out, _ = child.communicate(
            input=("asset_search sword\nasset_search\nquit\n").encode("utf-8"),
            timeout=60,
        )
        output = out.decode("utf-8", "replace")
        assert child.returncode == 0, output
        assert "asset_search OK: 2 result(s) for query 'sword'" in output, output
        assert "asset_search: no query given" in output, output
        assert server.bodies[0] == {"query": "sword", "maxPageSize": 5}, server.bodies
    print("OK  interactive REPL 'asset_search <query>' works; bare command is a gentle hint")


def scenario_cli_one_shot_recommend():
    with FakeStoreServer() as server:
        env = env_with_server(server.port)
        proc = run_cli(["--recommend-assets-once", "--query", "sword"], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert ("recommend_assets OK: ranked 2 of 2 result(s) for query 'sword' "
                "- #1 Steel Sword (score 2), #2 Wooden Sword (score 2)") in proc.stdout, proc.stdout
        assert "recommend_assets" in proc.stdout and "tools registered:" in proc.stdout, proc.stdout
        # One search request only: ranking adds no further API calls.
        assert server.total_calls == 1, server.total_calls
        assert server.bodies[0] == {"query": "sword", "maxPageSize": 5}, server.bodies

        # asset_type/max_results/creator/max-recommendations flow through.
        proc = run_cli(["--recommend-assets-once", "--query", "sword",
                        "--asset-type", "Model", "--max-results", "3",
                        "--creator", "Roblox", "--max-recommendations", "1"], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "- #1 Steel Sword" in proc.stdout and "- #2 " not in proc.stdout, proc.stdout
        assert server.bodies[-1] == {
            "query": "sword", "maxPageSize": 3, "searchCategoryType": "Model",
        }, server.bodies[-1]

        # --recommend-assets-once is a local HTTP call: no WebSocket server.
        assert "listening on ws://" not in proc.stdout, proc.stdout
        assert "load the RBXForge plugin" not in proc.stdout, proc.stdout
    print("OK  --recommend-assets-once success (exit 0, ranked log line, one search); "
          "type/max-results/creator/max-recommendations flags; no server start")


def scenario_cli_one_shot_recommend_errors():
    # Missing query -> usage error, exit 2, no HTTP call.
    with FakeStoreServer() as server:
        proc = run_cli(["--recommend-assets-once"], env_with_server(server.port))
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "--recommend-assets-once requires --query" in combined, combined
        assert server.total_calls == 0

    # Server error -> failure, exit 4.
    with FakeStoreServer(mode="error") as server:
        proc = run_cli(["--recommend-assets-once", "--query", "sword"],
                       env_with_server(server.port))
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 4, combined
        assert "recommend_assets FAILED" in combined, combined

    # No API key configured -> failure, exit 4.
    env = dict(os.environ)
    env.pop("RBXFORGE_OPEN_CLOUD_API_KEY", None)
    env.pop("ROBLOX_OPEN_CLOUD_API_KEY", None)
    proc = run_cli(["--recommend-assets-once", "--query", "sword"], env)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 4, combined
    assert "recommend_assets FAILED" in combined and "API key" in combined, combined

    # Unknown --asset-type is an argparse usage error (exit 2), nothing sent.
    with FakeStoreServer() as server:
        proc = run_cli(["--recommend-assets-once", "--query", "sword",
                        "--asset-type", "Bogus"], env_with_server(server.port))
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "invalid choice" in combined, combined
        assert server.total_calls == 0
    print("OK  --recommend-assets-once errors (missing --query / bogus type exit 2; "
          "server failure / no key exit 4)")


def scenario_cli_repl_recommend():
    with FakeStoreServer() as server:
        env = env_with_server(server.port)
        child = subprocess.Popen(
            [sys.executable, CLI],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        out, _ = child.communicate(
            input=("recommend_assets sword\nrecommend_assets\nquit\n").encode("utf-8"),
            timeout=60,
        )
        output = out.decode("utf-8", "replace")
        assert child.returncode == 0, output
        assert "recommend_assets OK:" in output, output
        assert "recommend_assets: no query given" in output, output
        assert server.total_calls == 1, server.total_calls
    print("OK  interactive REPL 'recommend_assets <query>' works; bare command is a gentle hint")


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


class _CapturingConsole:
    def __init__(self, lines):
        self.lines = lines

    def log(self, message):
        self.lines.append(message)

    def error(self, message):
        self.lines.append(message)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main():
    scenario_config()
    scenario_client_validation_and_clamps()
    scenario_search_success()
    scenario_search_truncated_and_clamp()
    scenario_search_asset_type_filter()
    scenario_search_defensive_parsing()
    scenario_search_rating_usage_metadata()
    scenario_search_response_shapes()
    scenario_search_rate_limit()
    scenario_search_connection_and_timeout()
    scenario_search_per_call_timeout()
    scenario_registry_exposes_asset_search()
    scenario_registry_exposes_recommend()
    scenario_tool_execution_injected_client()
    scenario_tool_execution_recommend()
    scenario_tool_execution_recommend_params()
    scenario_tool_execution_recommend_failures()
    scenario_tool_execution_failures()
    scenario_agent_exposure()
    scenario_asset_search_does_not_end_loop()
    scenario_agent_recommend_flow()
    scenario_agent_insert_flow()
    scenario_cli_one_shot_success()
    scenario_cli_one_shot_invalid_asset_type()
    scenario_cli_one_shot_errors()
    scenario_cli_one_shot_recommend()
    scenario_cli_one_shot_recommend_errors()
    scenario_cli_repl_asset_search()
    scenario_cli_repl_recommend()
    print("\nAll asset-discovery scenarios passed.")


if __name__ == "__main__":
    main()