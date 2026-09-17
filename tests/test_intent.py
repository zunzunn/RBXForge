"""Phase 9B tests: natural-language build intent and task decomposition.

These tests exercise ``cli/intent.decompose_intent`` and its integration with
the ToolRegistry / Agent loop.  They verify that higher-level requests are
broken into bounded, structured plans that use only the current toolset.
"""

import json
import os
import sys

# Add repository root to path so ``cli.intent`` is importable.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import cli.intent as intent
import cli.rbxforge as rbxforge


def _scene_with_spawn_and_house():
    return {
        "total_nodes": 5,
        "truncated": False,
        "landmarks": [
            {
                "name": "SpawnLocation",
                "class": "SpawnLocation",
                "path": "Workspace/SpawnLocation",
            },
            {"name": "Baseplate", "class": "Part", "path": "Workspace/Baseplate"},
        ],
        "models": [
            {
                "name": "House",
                "class": "Model",
                "path": "Workspace/House",
                "child_count": 4,
            },
            {
                "name": "Room",
                "class": "Model",
                "path": "Workspace/Room",
                "child_count": 3,
            },
        ],
        "groups": [],
        "class_counts": {"SpawnLocation": 1, "Part": 1, "Model": 2},
        "relevant": [],
    }


def scenario_simple_build_intent():
    """A simple build request decomposes into create_part steps."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("build a small modern shop here", summary)
    result = output["result"]
    assert result["action"] == "build", result
    assert "shop" in result["goal"].lower(), result
    assert any(r["name"] == "SpawnLocation" for r in result["reference_objects"]), (
        result
    )
    actions = result["required_actions"]
    assert 3 <= len(actions) <= 5, actions
    assert all(a["tool"] == "create_part" for a in actions), actions
    assert actions[0]["arguments"]["name"] == "Floor", actions
    assert actions[0]["arguments"].get("material") == "SmoothPlastic", actions
    assert result["spatial_constraints"], result
    assert result["verification_criteria"], result
    assert result["unsupported_capabilities"] == [], result
    print("OK  simple build intent decomposes into create_part steps")


def scenario_edit_intent():
    """An edit request decomposes into changes around an existing object."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("turn this room into a weapons shop", summary)
    result = output["result"]
    assert result["action"] == "edit", result
    assert any(r["name"] == "Room" for r in result["reference_objects"]), result
    actions = result["required_actions"]
    assert 1 <= len(actions) <= 5, actions
    assert all(a["tool"] == "create_part" for a in actions), actions
    assert result["verification_criteria"], result
    print("OK  edit intent decomposes around an existing reference")


def scenario_ambiguous_intent():
    """A vague request is classified ambiguous and produces no actions."""
    output = intent.decompose_intent("make it nicer", {})
    result = output["result"]
    assert result["action"] == "ambiguous", result
    assert result["required_actions"] == [], result
    assert result["reference_objects"] == [], result
    print("OK  ambiguous intent is reported safely")


def scenario_scene_dependent_intent():
    """A request that depends on an existing object picks the right reference."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("add a garage to the existing house", summary)
    result = output["result"]
    assert result["action"] == "build", result
    refs = result["reference_objects"]
    assert len(refs) == 1 and refs[0]["name"] == "House", result
    assert any("House" in sc for sc in result["spatial_constraints"]), result
    actions = result["required_actions"]
    names = {a["arguments"]["name"] for a in actions}
    assert "Floor" in names and "Walls" in names and "Roof" in names, names
    print("OK  scene-dependent intent references existing object")


def scenario_multi_object_goals():
    """A shop goal produces multiple dependent parts."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("build a shop", summary)
    result = output["result"]
    actions = result["required_actions"]
    assert len(actions) == 5, actions
    names = [a["arguments"]["name"] for a in actions]
    assert names == ["Floor", "Walls", "Roof", "Sign", "Counter"], names
    print("OK  multi-object goal produces five dependent parts")


def scenario_unsupported_request():
    """Unsupported capabilities are reported instead of hallucinated."""
    output = intent.decompose_intent("add particle effects to the shop", {})
    result = output["result"]
    assert result["action"] == "unsupported", result
    assert "particle" in result["unsupported_capabilities"], result
    assert result["required_actions"] == [], result
    print("OK  unsupported capabilities are rejected")


def scenario_dependency_ordering():
    """Structural steps declare sensible dependencies."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("build a house", summary)
    result = output["result"]
    actions = result["required_actions"]
    deps = result["dependencies"]
    assert len(deps) == len(actions), deps
    assert deps[0] == [], deps
    assert deps[1] == [0], deps
    assert deps[2] == [1], deps
    print("OK  dependency ordering chains structural steps")


def scenario_spatial_constraints():
    """Spatial references become explicit constraints."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("build a big garage beside the house", summary)
    result = output["result"]
    constraints = result["spatial_constraints"]
    assert any("House" in c for c in constraints), constraints
    assert any("large" in c for c in constraints), constraints
    print("OK  spatial constraints are captured")


def scenario_verification_criteria():
    """The plan includes concrete verification criteria."""
    summary = _scene_with_spawn_and_house()
    output = intent.decompose_intent("build a modern shop here", summary)
    result = output["result"]
    criteria = result["verification_criteria"]
    assert any("exists" in c for c in criteria), criteria
    assert any("SpawnLocation" in c for c in criteria), criteria
    print("OK  verification criteria are generated")


def scenario_bounded_plan_generation():
    """The intent layer never emits more than five actionable steps."""
    summary = _scene_with_spawn_and_house()
    for request in (
        "build a shop",
        "build a small modern house",
        "turn the room into a weapons shop",
        "add a garage to the house",
    ):
        output = intent.decompose_intent(request, summary)
        actions = output["result"]["required_actions"]
        assert len(actions) <= 5, (request, actions)
    print("OK  plan generation stays within five steps")


def scenario_registry_exposes_decompose_intent():
    """The tool registry exposes decompose_intent with metadata and schema."""
    registry = rbxforge.default_registry()
    tool = registry.get("decompose_intent")
    assert tool is not None
    assert isinstance(tool.description, str) and tool.description
    assert tool.input_schema["type"] == "object"
    assert set(tool.input_schema["required"]) == {"request"}
    assert tool.input_schema["properties"]["request"]["type"] == "string"
    assert tool.input_schema["properties"]["scene_summary"]["type"] == "object"

    # Execution is read-only and returns a structured plan.
    class FakeRBX:
        def __init__(self):
            self.requests = []
            self.logs = []

        def send_request(self, tool, params, timeout):
            self.requests.append((tool, params))
            return None

        def log(self, msg):
            self.logs.append(msg)

    rbx = FakeRBX()
    result = registry.execute(
        rbx,
        "decompose_intent",
        {
            "request": "build a small shop",
            "scene_summary": _scene_with_spawn_and_house(),
        },
    )
    assert result is not False, result
    plan = result.get("result") or {}
    assert plan.get("action") == "build", plan
    assert len(plan.get("required_actions") or []) <= 5
    assert rbx.requests == [], rbx.requests  # no plugin traffic
    print("OK  registry exposes decompose_intent as a read-only tool")


def main():
    scenario_simple_build_intent()
    scenario_edit_intent()
    scenario_ambiguous_intent()
    scenario_scene_dependent_intent()
    scenario_multi_object_goals()
    scenario_unsupported_request()
    scenario_dependency_ordering()
    scenario_spatial_constraints()
    scenario_verification_criteria()
    scenario_bounded_plan_generation()
    scenario_registry_exposes_decompose_intent()
    print("\nAll intent-decomposition scenarios passed.")


if __name__ == "__main__":
    main()
