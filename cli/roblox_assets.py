#!/usr/bin/env python3
"""RBXForge Roblox asset discovery - Phase 7A.

Searches the public Roblox Creator Store through the official Open Cloud
Creator Store API (BETA):

    POST https://apis.roblox.com/toolbox-service/v2/assets:search

Authentication uses an Open Cloud API key carried in the ``x-api-key`` header
(create the key on the Roblox create dashboard with the
``creator-store-product:read`` scope); the key is read from the environment
(``RBXFORGE_OPEN_CLOUD_API_KEY``, falling back to ``ROBLOX_OPEN_CLOUD_API_KEY``),
never hard-coded. Rate limit is 1000 requests/min per API key; HTTP 429 is
retried with exponential backoff (starting at 1s) a bounded number of times.

Strictly a READ-ONLY search: nothing is inserted, cloned, downloaded, or
purchased. It runs locally from this process - it does not use the Studio
plugin or the WebSocket protocol (the tool layer in cli/rbxforge.py routes it
through the ToolRegistry like any other tool, but execution is a local HTTP
call rather than a plugin ``request``).

Standard library only (``urllib``); no external dependencies.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://apis.roblox.com"
CREATOR_STORE_SEARCH_PATH = "/toolbox-service/v2/assets:search"

#: Environment variables consumed by :func:`asset_client_from_env`.
ASSET_SEARCH_API_KEY_ENV = "RBXFORGE_OPEN_CLOUD_API_KEY"
ASSET_SEARCH_API_KEY_ENV_FALLBACK = "ROBLOX_OPEN_CLOUD_API_KEY"
ASSET_SEARCH_BASE_URL_ENV = "RBXFORGE_OPEN_CLOUD_BASE_URL"
ASSET_SEARCH_TIMEOUT_ENV = "RBXFORGE_OPEN_CLOUD_TIMEOUT"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS = 20
MAX_QUERY_LENGTH = 200

#: Explicit User-Agent kept in sync with cli/providers.py RBXFORGE_USER_AGENT.
ASSET_SEARCH_USER_AGENT = "RBXForge/0.1.0"

#: The official Creator Store searchCategoryType allowlist (the only values the
#: /toolbox-service/v2/assets:search BETA accepts). Kept authoritative here so
#: both the tool schema (cli/rbxforge.py) and this client validate against the
#: same list.
ASSET_TYPES = [
    "Audio", "Model", "Decal", "Plugin", "MeshPart", "Video", "FontFamily",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class AssetError(Exception):
    """Base class for Roblox asset-search errors."""


class AssetConfigError(AssetError):
    """Raised when asset-search configuration is missing or invalid
    (no API key, or arguments outside the allowlist)."""


class AssetConnectionError(AssetError):
    """Raised when the Open Cloud endpoint cannot be reached."""


class AssetTimeoutError(AssetConnectionError):
    """Raised when the Open Cloud request times out."""


class AssetResponseError(AssetError):
    """Raised when the API returns an error or an unexpected response."""


class AssetRateLimitError(AssetError):
    """Raised when the API keeps returning HTTP 429 despite the retry budget."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class RobloxAssetClient:
    """Read-only search client for the Creator Store Open Cloud API.

    Configured with an Open Cloud API key and an optional base URL / timeout.
    ``base_url`` defaults to the official endpoint; tests override it to point
    at a fake in-process server. The key is only ever sent as the ``x-api-key``
    header and is never logged. On HTTP 429 the request is retried up to
    ``max_attempts`` times with exponential backoff from
    ``retry_backoff_base`` seconds.
    """

    def __init__(self, api_key, base_url=None, timeout=DEFAULT_TIMEOUT,
                 max_attempts=3, retry_backoff_base=1.0):
        if not api_key:
            raise AssetConfigError(
                "Roblox Open Cloud API key missing; set {0} (never hard-code "
                "credentials)".format(ASSET_SEARCH_API_KEY_ENV)
            )
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_backoff_base = retry_backoff_base

    def search(self, query, asset_type=None, max_results=DEFAULT_MAX_RESULTS,
               timeout=None):
        """Search the Creator Store and return a bounded structured result.

        ``query`` is required (non-empty, length-capped). ``asset_type`` is
        optional and must be one of :data:`ASSET_TYPES`. ``max_results`` is the
        result cap (1..20); the request always sends our own ``maxPageSize``.
        ``timeout`` overrides the configured per-request timeout for this call
        (None keeps the client's configured value). Returns a dict ``{query,
        asset_type, max_results, count, total, truncated, results}`` where each
        result is ``{asset_id, name, asset_type, creator, creator_id,
        description, thumbnail_url}`` plus optional ``rating``/``sales_count``/
        ``favorite_count`` usage metadata only when the API provided it.
        Raises an :class:`AssetError` subclass on any failure.
        """
        if not isinstance(query, str) or not query.strip():
            raise AssetConfigError("asset_search requires a non-empty query")
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH:
            raise AssetConfigError(
                "asset_search query too long (max {0} characters)".format(MAX_QUERY_LENGTH)
            )
        if timeout is not None and timeout <= 0:
            raise AssetConfigError("timeout must be positive, got: {0!r}".format(timeout))
        max_results = min(max(int(max_results), 1), MAX_RESULTS)
        if asset_type is not None and asset_type not in ASSET_TYPES:
            raise AssetConfigError(
                "unsupported asset_type {0!r}; must be one of {1}".format(
                    asset_type, ", ".join(ASSET_TYPES))
            )

        body = {"query": query, "maxPageSize": max_results}
        if asset_type is not None:
            body["searchCategoryType"] = asset_type
        data = self._post_json(body, timeout=timeout)
        return self._parse_search_response(data, query, asset_type, max_results)

    # -- transport --------------------------------------------------------- #

    def _post_json(self, body, timeout=None):
        url = self.base_url + CREATOR_STORE_SEARCH_PATH
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-api-key": self.api_key,
                "User-Agent": ASSET_SEARCH_USER_AGENT,
            },
            method="POST",
        )
        request_timeout = self.timeout if timeout is None else timeout
        backoff = self.retry_backoff_base
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                with exc:
                    detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code == 429 and attempt < self.max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                if exc.code == 429:
                    raise AssetRateLimitError(
                        "Creator Store rate limited (HTTP 429) after {0} "
                        "attempt(s): {1}".format(attempt, detail)
                    ) from exc
                raise AssetResponseError(
                    "HTTP {0} from Creator Store search: {1}".format(exc.code, detail)
                ) from exc
            except (TimeoutError, socket.timeout) as exc:
                # TimeoutError / socket.timeout covers timeouts on all supported
                # Python versions; must be caught before the generic OSError
                # below because TimeoutError is also an OSError subclass.
                raise AssetTimeoutError(
                    "Creator Store search timed out after {0:g}s".format(request_timeout)
                ) from exc
            except OSError as exc:
                raise AssetConnectionError(
                    "Creator Store search unreachable at {0}: {1}".format(url, exc)
                ) from exc
            try:
                return json.loads(raw)
            except ValueError as exc:
                raise AssetResponseError(
                    "Creator Store search returned non-JSON output"
                ) from exc
        raise AssetRateLimitError(
            "Creator Store search exhausted {0} attempt(s) with only HTTP 429 "
            "responses".format(self.max_attempts)
        )

    # -- response parsing -------------------------------------------------- #

    def _parse_search_response(self, data, query, asset_type, max_results):
        if not isinstance(data, dict):
            raise AssetResponseError(
                "Creator Store search returned a non-object response"
            )
        entries = data.get("creatorStoreAssets")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise AssetResponseError(
                "Creator Store response 'creatorStoreAssets' is not a list"
            )
        results = []
        for entry in entries:
            parsed = _parse_asset_entry(entry)
            if parsed is not None:
                results.append(parsed)
        count = len(results)
        total = data.get("totalResults")
        if not isinstance(total, int) or isinstance(total, bool):
            total = count
        return {
            "query": query,
            "asset_type": asset_type,
            "max_results": max_results,
            "count": count,
            "total": total,
            "truncated": total > count,
            "results": results,
        }


def _dig(target, *paths):
    """Return the first present, non-empty value along any of ``paths``.

    Each path is a tuple of keys (multi-level lookups such as
    ``("thumbnail", "url")``). Returns None when nothing resolves.
    """
    if not isinstance(target, dict):
        return None
    for path in paths:
        node = target
        ok = True
        for key in path:
            if not isinstance(node, dict) or key not in node:
                ok = False
                break
            node = node[key]
        if ok and node is not None and node != "":
            return node
    return None


def _stringify(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _numeric(value):
    """Return the numeric value of an API field if it is a usable number.

    Accepts int/float; rejects bools, strings, None and anything else so that
    malformed API payloads never propagate bogus numbers into ranking/tests.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _parse_asset_entry(entry):
    """Parse one CreatorStoreAsset item defensively.

    The official API types the ``asset`` field loosely (``Value``), so every
    field is read through fallbacks and unknown shapes are skipped rather than
    crashing. Only fields the API actually provided are included in the result.
    Returns None for entries that yield nothing usable.
    """
    if not isinstance(entry, dict):
        return None
    raw_asset = entry.get("asset")
    asset = raw_asset if isinstance(raw_asset, dict) else {}
    raw_creator = entry.get("creator")
    creator = raw_creator if isinstance(raw_creator, dict) else {}

    asset_id = _dig(asset, ("assetId",), ("id",), ("asset_id",))
    name = _dig(asset, ("name",), ("displayName",), ("assetName",))
    asset_type = _dig(asset, ("assetType",), ("type",), ("category",))
    description = _dig(asset, ("description",), ("assetDescription",))
    thumbnail_url = _dig(asset, ("thumbnailUrl",), ("thumbnail",))
    if isinstance(thumbnail_url, dict):
        thumbnail_url = _dig(thumbnail_url, ("url",), ("uri",), ("imageUrl",))

    creator_name = _dig(creator, ("displayName",), ("name",))
    creator_id = _dig(creator, ("creatorId",), ("id",))

    result = {}
    asset_id_text = _stringify(asset_id) if asset_id is not None else None
    if asset_id_text is not None:
        result["asset_id"] = asset_id_text
    name_text = _stringify(name) if name is not None else None
    if name_text is not None:
        result["name"] = name_text
    type_text = _stringify(asset_type) if asset_type is not None else None
    if type_text is not None:
        result["asset_type"] = type_text
    creator_text = _stringify(creator_name) if creator_name is not None else None
    creator_id_text = _stringify(creator_id) if creator_id is not None else None
    if creator_text is not None:
        result["creator"] = creator_text
    elif creator_id_text is not None:
        result["creator"] = creator_id_text
    if creator_id_text is not None:
        result["creator_id"] = creator_id_text
    description_text = _stringify(description) if description is not None else None
    if description_text is not None:
        result["description"] = description_text
    thumb_text = _stringify(thumbnail_url) if thumbnail_url is not None else None
    if thumb_text is not None:
        result["thumbnail_url"] = thumb_text

    # Optional rating/usage metadata (Phase 7B). Only added when the API gave a
    # usable number, so older results and the Phase 7A fixture assertions keep
    # their exact shape. The ranking layer treats these as modest bonuses.
    rating = _numeric(_dig(asset, ("rating",), ("averageRating",), ("assetRating",)))
    sales_count = _numeric(_dig(asset, ("salesCount",), ("totalSales",), ("sales",)))
    favorite_count = _numeric(_dig(asset, ("favorites",), ("favoriteCount",)))
    if rating is not None:
        result["rating"] = rating
    if sales_count is not None:
        result["sales_count"] = sales_count
    if favorite_count is not None:
        result["favorite_count"] = favorite_count
    return result or None


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _parse_timeout(value):
    if value in (None, ""):
        return DEFAULT_TIMEOUT
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise AssetConfigError(
            "{0} must be a number, got: {1!r}".format(ASSET_SEARCH_TIMEOUT_ENV, value)
        )
    if parsed <= 0:
        raise AssetConfigError(
            "{0} must be positive, got: {1!r}".format(ASSET_SEARCH_TIMEOUT_ENV, value)
        )
    return parsed


def asset_client_from_env(env=None):
    """Build a :class:`RobloxAssetClient` from the environment.

    Reads ``RBXFORGE_OPEN_CLOUD_API_KEY`` (falling back to
    ``ROBLOX_OPEN_CLOUD_API_KEY``), plus optional
    ``RBXFORGE_OPEN_CLOUD_BASE_URL`` and ``RBXFORGE_OPEN_CLOUD_TIMEOUT``
    overrides. Raises :class:`AssetConfigError` when no API key is configured
    (credentials are never hard-coded).
    """
    env = os.environ if env is None else env
    api_key = (
        env.get(ASSET_SEARCH_API_KEY_ENV) or env.get(ASSET_SEARCH_API_KEY_ENV_FALLBACK)
    )
    if not api_key:
        raise AssetConfigError(
            "Roblox Open Cloud API key missing; set {0} (never hard-code "
            "credentials)".format(ASSET_SEARCH_API_KEY_ENV)
        )
    return RobloxAssetClient(
        api_key=api_key,
        base_url=env.get(ASSET_SEARCH_BASE_URL_ENV),
        timeout=_parse_timeout(env.get(ASSET_SEARCH_TIMEOUT_ENV)),
    )