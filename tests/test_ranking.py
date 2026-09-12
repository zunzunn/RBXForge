#!/usr/bin/env python3
"""Asset ranking / recommendation tests (Phase 7B).

Covers cli/asset_ranking.py: the bounded, deterministic, explainable ranking
that turns Phase 7A Creator Store search results into recommendations. Tested
purely on in-memory result dicts (the parsed shape from
cli/roblox_assets.py) -- no HTTP, no plugin, nothing is downloaded/inserted/
purchased. The end-to-end wiring (search -> ranking -> agent -> CLI) lives in
tests/test_assets.py.

Run from the repository root:
    python3 tests/test_ranking.py
"""

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RANKING = os.path.join(ROOT, "cli", "asset_ranking.py")

KEY = "__ranked__"
SCORE = "score"
RANK = "rank"
REASON = "reason"
ASSET = "asset"


def load_ranking():
    spec = importlib.util.spec_from_file_location("rbxforge_ranking", RANKING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_ranking()
roblox_assets = mod.roblox_assets


def result(asset_id, name, asset_type="Model", creator="Roblox", description=None,
           rating=None, sales_count=None, favorite_count=None):
    """A result dict in the parsed Phase 7A shape (nothing else)."""
    entry = {
        "asset_id": str(asset_id),
        "name": name,
        "asset_type": asset_type,
        "creator": creator,
    }
    if description is not None:
        entry["description"] = description
    if rating is not None:
        entry["rating"] = rating
    if sales_count is not None:
        entry["sales_count"] = sales_count
    if favorite_count is not None:
        entry["favorite_count"] = favorite_count
    return entry


def recs(ranked):
    """Shortcut to the recommendation list."""
    return ranked["recommendations"]


def ranked_by_id(ranked):
    return [r["asset"]["asset_id"] for r in recs(ranked)]


def rank_of(ranked, asset_id):
    for r in recs(ranked):
        if r["asset"]["asset_id"] == asset_id:
            return r
    return None


def score_of(ranked, asset_id):
    entry = rank_of(ranked, asset_id)
    return entry[SCORE] if entry is not None else None


def assert_ranking_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        raise AssertionError("expected RankingError")
    except mod.RankingError:
        pass


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def scenario_ranking_basic():
    results = [
        result(1, "Shop Model", creator="Blocky Studios",
               description="A small shop for your game"),
        result(2, "House", creator="Roblox", description="A house"),
    ]
    ranked = mod.rank_assets(results, "buy a shop model")
    assert ranked["query"] == "buy a shop model"
    assert ranked["asset_type"] is None and ranked["creator"] is None
    assert ranked["evaluated"] == 2
    assert ranked["limit"] == mod.DEFAULT_MAX_RECOMMENDATIONS
    assert ranked_by_id(ranked) == ["1", "2"], ranked
    entry = rank_of(ranked, "1")
    assert entry[RANK] == 1
    # name hits "shop" + "model" (+2 each) + query type hint Model (+2) = 6
    assert entry[SCORE] == 6, entry
    assert "shop" in entry[REASON] and "Model" in entry[REASON], entry
    # The house gets only the type-hint bonus (+2): its description's "a" is a
    # stopword and carries no signal.
    assert score_of(ranked, "2") == 2
    assert ranked["tiebreak"] == mod.TIEBREAK_STRATEGY
    assert "read-only" in ranked["note"]
    assert ranked["count"] == 2
    print("OK  basic ranking: query tokens + type hint score higher; explainable reason")


def scenario_ranking_name_beats_description():
    # Description-only hit (+1) must rank below a title hit (+2) and a title
    # hit must outrank a description hit for the same term.
    results = [
        result(1, "Lava Lamp", description="glows warmly"),
        result(2, "Lamp Post", description="a street lamp"),
        result(3, "Rug", description="a warm lamp glow lamp"),
    ]
    ranked = mod.rank_assets(results, "lamp")
    by_id = ranked_by_id(ranked)
    assert by_id[:2] == ["2", "1"], ranked   # name hits first, tie broken by name
    assert score_of(ranked, "2") == 2 and score_of(ranked, "1") == 2
    assert score_of(ranked, "3") == 1, ranked  # description-only hit
    print("OK  name hit (+2) outranks description hit (+1); ties broken deterministically")


def scenario_ranking_phrase_bonus():
    results = [
        result(1, "Carnival Fun House", description="slots"),
        result(2, "Fun Home", description="carnival"),
    ]
    ranked = mod.rank_assets(results, "fun house")
    # Both hit "fun" and "house"? "Carnival Fun House" contains fun+house (both
    # name tokens) AND the phrase "fun house"; "Fun House" contains both name
    # tokens too. The longer title also matches the exact phrase -> +1 tiebreak.
    assert score_of(ranked, "1") > score_of(ranked, "2"), ranked
    assert "phrase" in rank_of(ranked, "1")[REASON], ranked
    print("OK  exact-phrase-in-title bonus applies; reason names the phrase")


def scenario_ranking_deterministic_across_input_order():
    results_fwd = [
        result(1, "Alpha", description="matches the query terms exactly"),
        result(2, "Beta", description="a completely different thing"),
    ]
    results_rev = [results_fwd[1], results_fwd[0]]
    fwd = mod.rank_assets(results_fwd, "query terms")
    rev = mod.rank_assets(results_rev, "query terms")
    assert [r[ASSET]["asset_id"] for r in recs(fwd)] == \
        [r[ASSET]["asset_id"] for r in recs(rev)], (fwd, rev)

    # Equal-score entries are ordered by name, then asset id -- independent of
    # the order the search returned them in.
    tied = results_fwd + [
        result(3, "Alpha", description="matches the query terms exactly"),  # same name, higher id
    ]
    ranked = mod.rank_assets(tied, "query terms")
    names = [r[ASSET]["name"] for r in recs(ranked)]
    assert names == ["Alpha", "Alpha", "Beta"], ranked
    ids = [r[ASSET]["asset_id"] for r in recs(ranked)]
    assert ids == ["1", "3", "2"], ranked  # same name -> id ascending
    print("OK  deterministic: input order does not change output; ties by name then asset id")


def scenario_ranking_missing_metadata():
    # Entries that are not result dicts are skipped, not fatal.
    results = [
        "junk", None, 42, {"asset_id": "no-name-but-id"},
        result(1, "Real Match", description="has real metadata"),
    ]
    ranked = mod.rank_assets(results, "real match")
    assert ranked["evaluated"] == 2, ranked   # both dict entries scored (partial gets 0)
    assert ranked_by_id(ranked)[0] == "1", ranked
    scored = rank_of(ranked, "1")
    assert scored["score"] == 5, scored  # name real+match (4) + phrase (1)
    # The partial dict scored 0 and gets a "no matching metadata" reason.
    partial = rank_of(ranked, "no-name-but-id")
    assert partial[SCORE] == 0 and "no matching" in partial[REASON], partial
    print("OK  non-dict entries skipped; missing/partial metadata tolerated")


def scenario_ranking_empty_results():
    ranked = mod.rank_assets([], "sword")
    assert ranked["evaluated"] == 0 and ranked["count"] == 0
    assert ranked["recommendations"] == []
    assert ranked["tiebreak"] and ranked["note"]
    print("OK  empty results produce an empty, well-formed ranking")


def scenario_ranking_invalid_inputs():
    assert_ranking_error(mod.rank_assets, "not-a-list", "sword")
    assert_ranking_error(mod.rank_assets, None, "sword")
    assert_ranking_error(mod.rank_assets, [], "")
    assert_ranking_error(mod.rank_assets, [], "   ")
    assert_ranking_error(mod.rank_assets, [], "!@# $%^")   # no searchable tokens
    assert_ranking_error(mod.rank_assets, [], "x" * (roblox_assets.MAX_QUERY_LENGTH + 1))
    assert_ranking_error(mod.rank_assets, [], "sword", asset_type="NotARealType")
    for bad_limit in ("x", True, False):
        assert_ranking_error(mod.rank_assets, [], "sword", limit=bad_limit)
    print("OK  invalid inputs rejected (non-list/empty/untokenizable/over-long/"
          "unknown type/bad limit)")


def scenario_ranking_limits():
    results = [result(i, "Match {0}".format(i), description="real match") for i in range(1, 8)]
    # Default caps at DEFAULT_MAX_RECOMMENDATIONS (3).
    ranked = mod.rank_assets(results, "real match")
    assert ranked["limit"] == mod.DEFAULT_MAX_RECOMMENDATIONS == 3
    assert ranked["count"] == 3 and len(recs(ranked)) == 3
    # Explicit larger limit is clamped to MAX_RECOMMENDATIONS (5).
    ranked = mod.rank_assets(results, "real match", limit=99)
    assert ranked["limit"] == mod.MAX_RECOMMENDATIONS == 5
    assert ranked["count"] == 5
    # limit below 1 clamps to 1.
    ranked = mod.rank_assets(results, "real match", limit=0)
    assert ranked["limit"] == 1 and ranked["count"] == 1
    print("OK  right-bounded limits: default 3, max 5, min 1")


def scenario_ranking_rating_and_usage():
    results = [
        result(1, "Old Sword", description="rusty"),
        result(2, "Polished Sword", description="rusty", rating=4.9, sales_count=120,
               favorite_count=300),
    ]
    ranked = mod.rank_assets(results, "rusty sword")
    # Both match "rusty" in the description (+1) and "sword" in the name (+2) =>
    # a base of 3 each; the usage metadata lifts #2 (rating bonus +2, sales +1,
    # favorites +1).
    assert score_of(ranked, "1") == 3
    assert score_of(ranked, "2") == 7, ranked
    reason = rank_of(ranked, "2")[REASON]
    assert "4.9/5 rating" in reason and "120 sales" in reason and "300 favorites" in reason
    print("OK  rating/usage metadata adds a capped bonus and appears in the reason")


def scenario_ranking_type_and_creator_bias():
    results = [
        result(1, "Spooky Audio", asset_type="Audio", creator="Soundify",
               description="ambient horror"),
        result(2, "Spooky Studio Prop", asset_type="Model", creator="Roblox",
               description="ambient horror for building"),
    ]
    # Query asks for "spooky audio"; the type synonym Audio adds +2 to #1.
    ranked = mod.rank_assets(results, "spooky audio")
    assert score_of(ranked, "1") > score_of(ranked, "2"), ranked
    assert "Audio" in rank_of(ranked, "1")[REASON]

    # An explicit creator biases toward that creator regardless of query order.
    ranked = mod.rank_assets(results, "spooky prop", creator="Soundify")
    assert ranked_by_id(ranked)[0] == "1", ranked
    assert "matches requested creator Soundify" in rank_of(ranked, "1")[REASON]
    print("OK  type synonym +2 and explicit creator match +2 bias the ranking")


def scenario_ranking_explicit_asset_type_flag():
    results = [
        result(1, "Quest Board", asset_type="Model", description="a board"),
        result(2, "Quest Board Song", asset_type="Audio", description="a tune"),
    ]
    # asset_type='Audio' adds +2 to the Audio asset even when the query text
    # does not name the type.
    ranked = mod.rank_assets(results, "quest board", asset_type="Audio")
    assert ranked_by_id(ranked)[0] == "2", ranked
    assert "requested 'Audio'" in rank_of(ranked, "2")[REASON]
    print("OK  explicit asset_type filter adds a type-match bonus")


def scenario_ranking_error_hierarchy():
    # RankingError extends the Phase 7A asset error base, so existing
    # asset_search error handling in the tool layer keeps catching it.
    assert issubclass(mod.RankingError, roblox_assets.AssetError)
    assert issubclass(mod.RankingError, Exception)
    print("OK  RankingError subclasses AssetError (unified asset error handling)")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main():
    scenario_ranking_basic()
    scenario_ranking_name_beats_description()
    scenario_ranking_phrase_bonus()
    scenario_ranking_deterministic_across_input_order()
    scenario_ranking_missing_metadata()
    scenario_ranking_empty_results()
    scenario_ranking_invalid_inputs()
    scenario_ranking_limits()
    scenario_ranking_rating_and_usage()
    scenario_ranking_type_and_creator_bias()
    scenario_ranking_explicit_asset_type_flag()
    scenario_ranking_error_hierarchy()
    print("\nAll ranking scenarios passed.")


if __name__ == "__main__":
    main()