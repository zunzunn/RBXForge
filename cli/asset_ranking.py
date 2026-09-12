#!/usr/bin/env python3
"""RBXForge asset ranking / recommendation - Phase 7B.

A bounded, deterministic, read-only ranking layer that turns the structured
Creator Store search results from cli/roblox_assets.py (Phase 7A) into an
ordered, *explainable* list of recommendations the AI can act on. It ranks
strictly from the metadata already returned by the search (name/title,
description, asset type, creator, rating/usage fields when the API provides
them, and query/keyword relevance) - it never downloads, inserts, purchases,
or modifies anything in Roblox Studio, and it never makes an API call of its
own.

Design rules:

- **Bounded:** the number of returned recommendations is capped at
  :data:`MAX_RECOMMENDATIONS`.
- **Deterministic:** equal-scoring assets are ordered by a fixed tiebreak
  (score desc, then name, then asset id), independent of the order the API
  returned them in, so the same metadata always yields the same ranking.
- **Defensive:** entries that are not usable result dicts are skipped rather
  than crashing; missing fields simply contribute nothing.
- **Explainable:** every recommendation carries a human-readable ``reason``
  listing which metadata matched (title terms, description terms, phrase,
  asset type, creator, usage), so the Agent can state why an asset was chosen.
- **Typed errors:** invalid inputs raise :class:`RankingError` (a subclass of
  :class:`roblox_assets.AssetError`, so the existing asset-search error
  handling in cli/rbxforge.py keeps catching it).

Scoring is the sum of small integer weights (see the constants below), so a
name match outranks a description match, marketplace relevance from a query
type hint is rewarded, and rating/usage metadata only ever adds a modest bonus.
The marketplace search already orders by its own relevance on the wire; this
layer re-scores that snapshot purely from its metadata.

Standard library only; no external dependencies.
"""

import os
import re
import sys

# cli/ is not a package, so this module locates its sibling cli/roblox_assets.py
# (for ASSET_TYPES / MAX_QUERY_LENGTH and the AssetError base class) directly.
_HERE = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import roblox_assets  # noqa: E402


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class RankingError(roblox_assets.AssetError):
    """Raised when ranking receives invalid inputs (non-list results, an empty
    or untokenizable query, an unknown asset_type, a non-numeric limit, ...)."""


# --------------------------------------------------------------------------- #
# Scoring configuration (small integer weights, single source of truth)
# --------------------------------------------------------------------------- #

#: Default number of recommendations returned when the caller omits ``limit``.
DEFAULT_MAX_RECOMMENDATIONS = 3

#: Hard cap on recommendations per call (results stay bounded).
MAX_RECOMMENDATIONS = 5

#: Token-level matches.
NAME_TOKEN_HIT = 2          # query token appears in the asset name/title
DESCRIPTION_TOKEN_HIT = 1   # query token appears in the asset description
PHRASE_HIT = 1              # the whole query phrase appears in the title

#: Type / creator signals.
ASSET_TYPE_TOKEN_HINT = 2   # a query type synonym ("model", "decal", ...) equals the asset_type
CREATOR_EXPLICIT_HIT = 2    # the requested creator matches the asset's creator
CREATOR_TOKEN_HIT = 1       # a query token appears in the creator name

#: Rating/usage metadata bonuses (only ever applied when the API provided the
#: field, and only a modest, capped amount so metadata-rich but irrelevant
#: assets cannot outrank clear query matches).
USAGE_PRESENT_BONUS = 1     # any positive usage metadata (rating > 0, sales, favorites)
HIGH_RATING_BONUS = 2       # rating >= HIGH_RATING_THRESHOLD
HIGH_RATING_THRESHOLD = 4.0

#: Deterministic tiebreak used after the primary (descending) score.
TIEBREAK_STRATEGY = "score (descending), then asset name, then asset id"

#: Query-token synonyms for the official searchCategoryType values. A query like
#: "find me a shop model" contributes an ASSET_TYPE_TOKEN_HINT to Model assets.
TYPE_SYNONYMS = {
    "model": "Model", "models": "Model",
    "mesh": "MeshPart", "meshes": "MeshPart", "meshpart": "MeshPart",
    "audio": "Audio", "music": "Audio", "sound": "Audio", "sounds": "Audio",
    "sfx": "Audio",
    "decal": "Decal", "decals": "Decal",
    "plugin": "Plugin", "plugins": "Plugin",
    "video": "Video", "videos": "Video",
    "font": "FontFamily", "fonts": "FontFamily", "fontfamily": "FontFamily",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: High-frequency words that carry no ranking signal (articles, prepositions,
#: helper verbs). Excluded from token-match scoring so noisy queries like
#: "find me a shop model" do not give every asset a spurious description hit.
#: The exact-phrase bonus still uses the raw query, and type synonyms are
#: unaffected.
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
    "is", "are", "was", "were", "be", "been", "it", "this", "that", "these",
    "those", "with", "from", "as", "by", "me", "i", "you", "my", "your",
    "we", "our", "us", "do", "does", "did", "not", "no",
})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _query_tokens(query):
    """Lowercased alphanumeric tokens from a validated query string.

    Raises :class:`RankingError` for empty / over-long / untokenizable queries
    so callers never feed an unsearchable query to the scorer.
    """
    if not isinstance(query, str) or not query.strip():
        raise RankingError("asset ranking requires a non-empty query string")
    query = query.strip()
    if len(query) > roblox_assets.MAX_QUERY_LENGTH:
        raise RankingError(
            "asset ranking query too long (max {0} characters)".format(
                roblox_assets.MAX_QUERY_LENGTH)
        )
    tokens = _TOKEN_RE.findall(query.lower())
    if not tokens:
        raise RankingError(
            "asset ranking query has no searchable tokens: {0!r}".format(query)
        )
    return tokens


def _text(value):
    """Lowercase text of a string field, or '' when missing/not a string."""
    return value.lower() if isinstance(value, str) else ""


def _type_hint_from_query(tokens):
    """Return the asset type a query token names (via TYPE_SYNONYMS), or None."""
    for token in tokens:
        aliased = TYPE_SYNONYMS.get(token)
        if aliased is not None:
            return aliased
    return None


def _same_type(asset_type, expected):
    return (
        isinstance(asset_type, str)
        and asset_type.lower() == expected.lower()
    )


def _creator_match(asset, creator):
    """True when the asset's creator equals/contains the requested creator."""
    creator = (creator or "").strip().lower()
    if not creator:
        return False
    asset_creator = _text(asset.get("creator"))
    return bool(asset_creator) and (creator in asset_creator or asset_creator in creator)


def _usage_bonus(asset):
    """Deterministic, capped bonus from any rating/usage metadata the API
    returned. Returns ``{"bonus": int, "detail": str}``; ``detail`` is empty
    when no usage metadata was available."""
    bonus = 0
    bits = []

    rating = asset.get("rating")
    if isinstance(rating, bool):
        rating = None
    if rating is not None:
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            rating = None
    if rating is not None:
        if rating >= HIGH_RATING_THRESHOLD:
            bonus += HIGH_RATING_BONUS
        elif rating > 0:
            bonus += USAGE_PRESENT_BONUS
        bits.append("{0:g}/5 rating".format(rating))

    for key, label in (("sales_count", "sales"), ("favorite_count", "favorites")):
        value = asset.get(key)
        if isinstance(value, bool):
            value = None
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        if value is not None and value > 0:
            bonus += USAGE_PRESENT_BONUS
            bits.append("{0:g} {1}".format(value, label))

    return {"bonus": bonus, "detail": ", ".join(bits)}


def _score_asset(asset, query_text, tokens, asset_type_explicit, creator_explicit,
                 type_token_hint):
    """Score one asset against the validated query.

    Returns ``(score, signals)`` where ``signals`` is a dict describing *which*
    metadata matched (used for the human-readable reason).
    """
    if not isinstance(asset, dict):
        return 0, {}
    name = _text(asset.get("name"))
    description = _text(asset.get("description"))
    asset_type = asset.get("asset_type")

    signals = {}
    score = 0

    # Title / phrase relevance.
    name_hits = [token for token in tokens if token not in STOPWORDS and token in name]
    if name_hits:
        score += NAME_TOKEN_HIT * len(name_hits)
        signals["name"] = name_hits
    if len(tokens) >= 2 and query_text in name:
        score += PHRASE_HIT
        signals["phrase"] = query_text

    # Description relevance (tokens already counted in the title are not
    # double-counted here; stopwords never count).
    desc_hits = [
        token for token in tokens
        if token not in STOPWORDS and token not in name and token in description
    ]
    if desc_hits:
        score += DESCRIPTION_TOKEN_HIT * len(desc_hits)
        signals["description"] = desc_hits

    # Asset type relevance.
    if type_token_hint is not None and _same_type(asset_type, type_token_hint):
        score += ASSET_TYPE_TOKEN_HINT
        signals["asset_type"] = "type matches query hint {0!r}".format(type_token_hint)
    elif asset_type_explicit is not None and _same_type(asset_type, asset_type_explicit):
        score += ASSET_TYPE_TOKEN_HINT
        signals["asset_type"] = "type matches requested {0!r}".format(asset_type_explicit)

    # Creator relevance.
    if creator_explicit and _creator_match(asset, creator_explicit):
        score += CREATOR_EXPLICIT_HIT
        signals["creator"] = ["matches requested creator {0}".format(creator_explicit)]
    else:
        creator_l = _text(asset.get("creator"))
        creator_hits = [token for token in tokens if token not in STOPWORDS and token in creator_l]
        if creator_hits:
            score += CREATOR_TOKEN_HIT * len(creator_hits)
            signals["creator"] = ["name matches creator term(s): {0}".format(
                ", ".join(creator_hits))]

    # Rating/usage metadata bonus (only when the API provided it).
    usage = _usage_bonus(asset)
    if usage["bonus"]:
        score += usage["bonus"]
        signals["usage"] = usage["detail"]

    return score, signals


def _reason(name, signals):
    """Build the human-readable reason for one recommendation."""
    parts = []
    if signals.get("phrase"):
        parts.append("title contains the phrase {0!r}".format(signals["phrase"]))
    name_hits = signals.get("name")
    if name_hits:
        parts.append("title matches term(s): {0}".format(", ".join(name_hits)))
    desc_hits = signals.get("description")
    if desc_hits:
        parts.append("description matches term(s): {0}".format(", ".join(desc_hits)))
    if signals.get("asset_type"):
        parts.append(signals["asset_type"])
    for creator_bit in signals.get("creator", []):
        parts.append(creator_bit)
    if signals.get("usage"):
        parts.append("usage metadata: {0}".format(signals["usage"]))
    return "; ".join(parts) if parts else "no matching metadata"


def _limit_for(limit):
    """Clamp the requested recommendation limit to [1, MAX_RECOMMENDATIONS]."""
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        raise RankingError("asset ranking limit must be a whole number, got: {0!r}".format(limit))
    if isinstance(limit, bool):
        raise RankingError("asset ranking limit must be a whole number, got: {0!r}".format(limit))
    return min(max(parsed, 1), MAX_RECOMMENDATIONS)


# --------------------------------------------------------------------------- #
# Ranking entry point
# --------------------------------------------------------------------------- #


def rank_assets(results, query, asset_type=None, creator=None,
                limit=DEFAULT_MAX_RECOMMENDATIONS):
    """Rank ``results`` (from :meth:`roblox_assets.RobloxAssetClient.search`)
    against ``query`` deterministically and return a bounded, explainable set of
    recommendations.

    ``results`` must be a list of asset result dicts (the ``results`` list from
    a Phase 7A search). ``query`` is the same query the search was run with.
    ``asset_type`` / ``creator`` are optional filters/hints (``asset_type``
    must be one of :data:`roblox_assets.ASSET_TYPES`). ``limit`` is the bound
    on returned recommendations (clamped to 1..:data:`MAX_RECOMMENDATIONS`).

    Returns a dict::

        {
            "query": ..., "asset_type": ..., "creator": ...,
            "evaluated": <number of usable result dicts scored>,
            "limit": <resolved recommendation cap>,
            "count": <number of recommendations returned>,
            "recommendations": [
                {"rank": 1, "score": N,
                 "reason": "title matches term(s): shop",
                 "asset": { ...original result dict... }},
                ...
            ],
            "tiebreak": TIEBREAK_STRATEGY,
            "note": "read-only ranking ...",
        }

    Raises :class:`RankingError` for invalid inputs (non-list results, missing
    / over-long / untokenizable query, unknown ``asset_type``, non-numeric
    ``limit``). Unusable (non-dict) entries in ``results`` are skipped.
    """
    if not isinstance(results, list):
        raise RankingError(
            "asset ranking requires a results list, got: {0!r}".format(type(results).__name__)
        )
    tokens = _query_tokens(query)
    query_text = query.strip().lower()

    if asset_type is not None and asset_type not in roblox_assets.ASSET_TYPES:
        raise RankingError(
            "unsupported asset_type {0!r}; must be one of {1}".format(
                asset_type, ", ".join(roblox_assets.ASSET_TYPES))
        )
    limit = _limit_for(limit)
    creator = (creator or "").strip() or None

    type_token_hint = _type_hint_from_query(tokens)

    scored = []
    for item in results:
        if not isinstance(item, dict):
            continue
        score, signals = _score_asset(
            item, query_text, tokens, asset_type, creator, type_token_hint
        )
        scored.append((score, signals, item))

    # Deterministic ordering: primary score descending, then name, then id --
    # independent of the order the API returned the results in.
    def sort_key(entry):
        score, _signals, item = entry
        return (-score, _text(item.get("name")), str(item.get("asset_id", "")))

    scored.sort(key=sort_key)
    ranked = scored[:limit]

    recommendations = []
    for index, (score, signals, item) in enumerate(ranked, start=1):
        recommendations.append({
            "rank": index,
            "score": score,
            "reason": _reason(item, signals),
            "asset": item,
        })

    return {
        "query": query_text,
        "asset_type": asset_type,
        "creator": creator,
        "evaluated": len(scored),
        "limit": limit,
        "count": len(recommendations),
        "recommendations": recommendations,
        "tiebreak": TIEBREAK_STRATEGY,
        "note": "read-only: ranked from the returned metadata only; "
                "nothing is downloaded, inserted, or purchased",
    }