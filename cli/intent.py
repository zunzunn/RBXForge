"""Phase 9B: natural-language build intent and task decomposition.

``decompose_intent`` translates higher-level build/edit requests into a
structured, bounded plan that uses only the tools RBXForge already implements.
It is intentionally lightweight and rule-based: it recognizes a small set of
action verbs, target structures, spatial references, and style/size modifiers,
then emits a plan containing only actions the current toolset can perform.
Unsupported capabilities are reported explicitly rather than hallucinated.
"""

import re

# Action verbs the intent layer understands.  Verbs can appear in multiple
# sets because context (target, reference) disambiguates them.
_BUILD_VERBS = {"build", "create", "construct", "spawn", "place"}
_EDIT_VERBS = {"turn", "change", "convert", "transform", "make", "modify",
               "detail", "upgrade", "expand", "renovate"}
_ADD_VERBS = {"add", "attach", "append", "extend"}

# Structures we can decompose into concrete parts.  Each entry defines the
# default parts produced when building that structure from scratch.
_STRUCTURES = {
    "shop": {
        "parts": ["Floor", "Walls", "Roof", "Sign", "Counter"],
        "category": "building",
    },
    "house": {
        "parts": ["Floor", "Walls", "Roof", "Door", "Window"],
        "category": "building",
    },
    "garage": {
        "parts": ["Floor", "Walls", "Roof", "Door"],
        "category": "building",
    },
    "room": {
        "parts": ["Floor", "Walls", "Ceiling"],
        "category": "building",
    },
    "tower": {
        "parts": ["Base", "MidSection", "Top"],
        "category": "building",
    },
    "bridge": {
        "parts": ["Deck", "PillarA", "PillarB"],
        "category": "structure",
    },
}

# Style/size modifiers that map to create_part-compatible properties.
_STYLE_MAP = {
    "modern": {"material": "SmoothPlastic", "color": "gray"},
    "wooden": {"material": "Wood", "color": "red"},
    "medieval": {"material": "Slate", "color": "gray"},
    "stone": {"material": "Slate", "color": "gray"},
    "metal": {"material": "Metal", "color": "gray"},
    "neon": {"material": "Neon", "color": "blue"},
    "red": {"color": "red"},
    "blue": {"color": "blue"},
    "green": {"color": "green"},
    "yellow": {"color": "yellow"},
    "white": {"color": "white"},
    "black": {"color": "black"},
    "gray": {"color": "gray"},
    "small": {"size_factor": 0.7},
    "big": {"size_factor": 1.4},
    "large": {"size_factor": 1.6},
    "tiny": {"size_factor": 0.5},
}

# Capabilities the current toolset cannot perform.  Requests mentioning these
# are flagged as unsupported instead of being silently ignored.
_UNSUPPORTED_TERMS = {
    "particle", "particles", "terrain", "lighting", "skybox", "animation",
    "animations", "rig", "rigging", "mesh", "ui", "gui", "screen", "shirt",
    "pants", "decal", "texture", "textures", "paint", "painting",
}

# Supported create_part colors and materials for validation.
_SUPPORTED_COLORS = {"red", "blue", "green", "yellow", "white", "black", "gray"}
_SUPPORTED_MATERIALS = {
    "Plastic", "SmoothPlastic", "Neon", "Wood", "WoodPlanks", "Metal",
    "DiamondPlate", "Concrete", "Brick", "Glass", "Granite", "Marble",
    "Slate", "Sand", "Fabric", "Grass", "Ice",
}

# Maximum number of actionable steps the intent layer will ever emit.
# This aligns with the plan_build limit and the build-mode tool budget.
_MAX_PLAN_STEPS = 5


def _normalize(text):
    """Lowercase and strip punctuation for matching."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9_\s]", "", text.lower())


def _tokens(text):
    return _normalize(text).split()


def _find_reference(tokens, scene_summary):
    """Find an existing object in the scene summary referenced by the request.

    Looks for patterns like "the house", "this room", "existing shop",
    "near the tree", "beside SpawnLocation", etc.  Returns the first matching
    landmark/model/group entry or None.
    """
    if not scene_summary:
        return None

    reference_indicators = {"the", "this", "that", "existing", "near", "beside",
                            "next to", "by", "to", "of", "into", "from"}
    candidates = []
    for group in ("landmarks", "models", "groups", "relevant"):
        for item in scene_summary.get(group) or []:
            name = item.get("name") or ""
            if not name:
                continue
            candidates.append((name.lower(), item))

    # Prefer matches preceded by a reference indicator in the token stream.
    for i, tok in enumerate(tokens):
        if tok in reference_indicators:
            for name, item in candidates:
                if name in tokens[i + 1:]:
                    return item
                # allow compound names like "big tree" split across tokens
                name_tokens = name.split()
                if len(name_tokens) > 1:
                    window = tokens[i + 1:i + 1 + len(name_tokens)]
                    if window == name_tokens:
                        return item

    # Fallback: direct name match anywhere in the request.
    for name, item in candidates:
        if name in tokens:
            return item
        name_tokens = name.split()
        if len(name_tokens) > 1 and any(
            tokens[j:j + len(name_tokens)] == name_tokens
            for j in range(len(tokens) - len(name_tokens) + 1)
        ):
            return item

    return None


def _find_spawn_location(scene_summary):
    """Return the SpawnLocation landmark if present."""
    for item in scene_summary.get("landmarks") or []:
        if item.get("name") == "SpawnLocation" or item.get("class") == "SpawnLocation":
            return item
    return None


def _extract_target(tokens):
    """Find a known structure type in the request tokens."""
    for name in _STRUCTURES:
        if name in tokens:
            return name
    # Accept plural-ish forms.
    if "shops" in tokens:
        return "shop"
    if "houses" in tokens:
        return "house"
    if "garages" in tokens:
        return "garage"
    if "rooms" in tokens:
        return "room"
    if "towers" in tokens:
        return "tower"
    if "bridges" in tokens:
        return "bridge"
    return None


def _extract_style(tokens):
    """Extract style/size modifiers that map to supported create_part props."""
    style = {"size_factor": 1.0}
    for tok in tokens:
        if tok in _STYLE_MAP:
            style.update(_STYLE_MAP[tok])
    return style


def _check_unsupported(tokens):
    """Return a list of unsupported capabilities mentioned in the request."""
    found = []
    for tok in tokens:
        if tok in _UNSUPPORTED_TERMS:
            found.append(tok)
    return sorted(set(found))


def _position_for_step(index, size_factor, reference_item, total_steps):
    """Produce a deterministic, illustrative position for a plan step.

    Positions are relative to a default origin when no reference is known.
    Spatial constraints (returned separately) describe the intended placement
    relative to real scene objects; execution can refine positions after
    inspecting the reference object.
    """
    base = {"x": 0, "y": 0, "z": 0}
    if reference_item:
        # Place slightly offset from the reference object's path.
        base = {"x": 5, "y": 0, "z": 5}

    spacing = 4 * size_factor
    offsets = [
        {"x": 0, "y": 0, "z": 0},
        {"x": 0, "y": spacing, "z": 0},
        {"x": 0, "y": spacing * 2, "z": 0},
        {"x": spacing, "y": spacing, "z": 0},
        {"x": -spacing, "y": spacing / 2, "z": 0},
    ]
    off = offsets[index % len(offsets)]
    return {
        "x": base["x"] + off["x"],
        "y": base["y"] + off["y"],
        "z": base["z"] + off["z"],
    }


def _size_for_part(part_name, size_factor):
    """Return an illustrative size for a named structural part."""
    part_lower = part_name.lower()
    if "floor" in part_lower or "deck" in part_lower or "base" in part_lower:
        return {"x": 12 * size_factor, "y": 1, "z": 12 * size_factor}
    if "wall" in part_lower or "walls" in part_lower:
        return {"x": 12 * size_factor, "y": 8 * size_factor, "z": 1}
    if "roof" in part_lower or "ceiling" in part_lower or "top" in part_lower:
        return {"x": 13 * size_factor, "y": 1, "z": 13 * size_factor}
    if "door" in part_lower:
        return {"x": 4 * size_factor, "y": 6 * size_factor, "z": 1}
    if "window" in part_lower:
        return {"x": 3 * size_factor, "y": 3 * size_factor, "z": 1}
    if "sign" in part_lower:
        return {"x": 4 * size_factor, "y": 2 * size_factor, "z": 1}
    if "counter" in part_lower or "shelf" in part_lower:
        return {"x": 6 * size_factor, "y": 2 * size_factor, "z": 2 * size_factor}
    if "pillar" in part_lower:
        return {"x": 2 * size_factor, "y": 10 * size_factor, "z": 2 * size_factor}
    return {"x": 4 * size_factor, "y": 4 * size_factor, "z": 4 * size_factor}


def _build_actions(structure, style, reference_item):
    """Generate create_part actions for a structure template."""
    parts = _STRUCTURES[structure]["parts"]
    size_factor = style.get("size_factor", 1.0)
    actions = []
    for idx, part in enumerate(parts[:_MAX_PLAN_STEPS]):
        args = {
            "name": part,
            "position": _position_for_step(idx, size_factor, reference_item, len(parts)),
            "size": _size_for_part(part, size_factor),
            "anchored": True,
            "can_collide": True,
        }
        color = style.get("color")
        if color in _SUPPORTED_COLORS:
            args["color"] = color
        material = style.get("material")
        if material in _SUPPORTED_MATERIALS:
            args["material"] = material

        depends_on = []
        if idx > 0:
            # Simple vertical dependency chain for structural parts.
            depends_on.append(idx - 1)

        actions.append({
            "tool": "create_part",
            "arguments": args,
            "reason": "{0} for the {1}".format(part, structure),
            "depends_on": depends_on,
        })
    return actions


def _detail_actions(reference_item, style):
    """Generate create_part actions that add detail around an existing object."""
    size_factor = style.get("size_factor", 1.0)
    parts = ["Trim", "Window", "Awning"] if "shop" in (reference_item or {}).get("name", "").lower() else ["Trim", "Window", "PorchLight"]
    actions = []
    for idx, part in enumerate(parts[:_MAX_PLAN_STEPS]):
        args = {
            "name": part,
            "position": _position_for_step(idx, size_factor, reference_item, len(parts)),
            "size": _size_for_part(part, size_factor),
            "anchored": True,
            "can_collide": True,
        }
        color = style.get("color")
        if color in _SUPPORTED_COLORS:
            args["color"] = color
        material = style.get("material")
        if material in _SUPPORTED_MATERIALS:
            args["material"] = material
        actions.append({
            "tool": "create_part",
            "arguments": args,
            "reason": "add {0} detail".format(part),
            "depends_on": [],
        })
    return actions


def _convert_actions(target, reference_item, style):
    """Generate actions for converting an existing object into a new use.

    Keeps the reference object and adds the functional pieces the current
    toolset can actually create.
    """
    size_factor = style.get("size_factor", 1.0)
    actions = []
    if "weapon" in target or "shop" in target:
        parts = ["WeaponCounter", "WeaponRack", "Sign"]
    elif "house" in target or "room" in target:
        parts = ["Window", "Door", "Trim"]
    else:
        parts = ["Counter", "Sign"]
    for idx, part in enumerate(parts[:_MAX_PLAN_STEPS - 1]):
        args = {
            "name": part,
            "position": _position_for_step(idx, size_factor, reference_item, len(parts)),
            "size": _size_for_part(part, size_factor),
            "anchored": True,
            "can_collide": True,
        }
        color = style.get("color")
        if color in _SUPPORTED_COLORS:
            args["color"] = color
        material = style.get("material")
        if material in _SUPPORTED_MATERIALS:
            args["material"] = material
        actions.append({
            "tool": "create_part",
            "arguments": args,
            "reason": "convert space into a {0}".format(target),
            "depends_on": [],
        })
    return actions


def classify_intent(request):
    """Classify a natural-language request into a lightweight intent sketch."""
    tokens = _tokens(request)
    if not tokens:
        return {"action": "ambiguous", "target": None, "reference": None,
                "modifiers": [], "confidence": 0.0}

    unsupported = _check_unsupported(tokens)
    if unsupported:
        return {"action": "unsupported", "target": None, "reference": None,
                "modifiers": unsupported, "confidence": 1.0}

    has_build = any(t in _BUILD_VERBS for t in tokens)
    has_edit = any(t in _EDIT_VERBS for t in tokens)
    has_add = any(t in _ADD_VERBS for t in tokens)
    target = _extract_target(tokens)
    style = _extract_style(tokens)
    modifiers = [k for k in style if k != "size_factor"]

    # Disambiguate "make": build if a new structure is mentioned, edit if an
    # existing reference is implied and no new structure type is given.
    if not has_build and not has_edit and not has_add:
        if target:
            has_build = True
        else:
            return {"action": "ambiguous", "target": None, "reference": None,
                    "modifiers": modifiers, "confidence": 0.3}

    # An explicit "add <structure> to <existing>" is treated as a build/add.
    if has_add and target:
        return {"action": "build", "target": target, "reference": None,
                "modifiers": modifiers, "confidence": 0.9}

    # Edit/conversion when we have an existing reference but no new structure.
    if has_edit and not target:
        return {"action": "edit", "target": None, "reference": "implied",
                "modifiers": modifiers, "confidence": 0.7}

    # Build a new structure.
    if has_build and target:
        return {"action": "build", "target": target, "reference": None,
                "modifiers": modifiers, "confidence": 0.9}

    # Edit/conversion of an existing object into a new structure.
    if has_edit and target:
        return {"action": "edit", "target": target, "reference": "implied",
                "modifiers": modifiers, "confidence": 0.8}

    return {"action": "ambiguous", "target": target, "reference": None,
            "modifiers": modifiers, "confidence": 0.4}


def decompose_intent(request, scene_summary=None):
    """Translate a natural-language request into a bounded, structured plan.

    ``scene_summary`` is the dict returned by ``analyze_scene`` (the ``result``
    field).  The returned plan contains only actions the current toolset can
    perform; unsupported capabilities are reported in
    ``unsupported_capabilities``.
    """
    scene_summary = scene_summary or {}
    tokens = _tokens(request)
    classification = classify_intent(request)
    action = classification["action"]
    target = classification["target"]
    style = _extract_style(tokens)
    reference_item = _find_reference(tokens, scene_summary)

    # Spatial default: if the user said "here" and a SpawnLocation exists, use it.
    if "here" in tokens and not reference_item:
        reference_item = _find_spawn_location(scene_summary)

    if action == "unsupported":
        return {
            "ok": True,
            "result": {
                "goal": request,
                "action": "unsupported",
                "reference_objects": [],
                "required_actions": [],
                "dependencies": [],
                "spatial_constraints": [],
                "verification_criteria": [],
                "unsupported_capabilities": classification["modifiers"],
                "note": "These requested capabilities are not available yet.",
            },
        }

    if action == "ambiguous":
        return {
            "ok": True,
            "result": {
                "goal": request,
                "action": "ambiguous",
                "reference_objects": [],
                "required_actions": [],
                "dependencies": [],
                "spatial_constraints": [],
                "verification_criteria": [],
                "unsupported_capabilities": [],
                "note": "The request is too vague. Try specifying a structure "
                        "(shop, house, garage, room) and a location.",
            },
        }

    # Build/add: create a new structure from a template.
    if action == "build" and target:
        actions = _build_actions(target, style, reference_item)
        spatial = []
        if reference_item:
            spatial.append("build near {0}".format(reference_item.get("name")))
        else:
            spatial.append("build at default location")
        if "small" in tokens or "tiny" in tokens:
            spatial.append("small scale")
        if "big" in tokens or "large" in tokens:
            spatial.append("large scale")

        verification = ["every created part exists"]
        if reference_item:
            verification.append("{0} still exists".format(reference_item.get("name")))

        return {
            "ok": True,
            "result": {
                "goal": request,
                "action": "build",
                "reference_objects": [reference_item] if reference_item else [],
                "required_actions": actions,
                "dependencies": [a["depends_on"] for a in actions],
                "spatial_constraints": spatial,
                "verification_criteria": verification,
                "unsupported_capabilities": [],
                "note": "Ready to build a {0} using up to {1} create_part steps.".format(
                    target, len(actions)),
            },
        }

    # Edit: modify/convert an existing object.
    if action == "edit":
        if not reference_item:
            return {
                "ok": True,
                "result": {
                    "goal": request,
                    "action": "ambiguous",
                    "reference_objects": [],
                    "required_actions": [],
                    "dependencies": [],
                    "spatial_constraints": [],
                    "verification_criteria": [],
                    "unsupported_capabilities": [],
                    "note": "Edit requests need a reference object (e.g. 'this room', "
                            "'the house').",
                },
            }

        # If a new structure type is requested, convert; otherwise detail.
        if target:
            actions = _convert_actions(target, reference_item, style)
        else:
            actions = _detail_actions(reference_item, style)

        spatial = ["modify or add near {0}".format(reference_item.get("name"))]
        verification = ["{0} still exists".format(reference_item.get("name"))]
        verification.extend("created part '{0}' exists".format(a["arguments"]["name"])
                             for a in actions)

        return {
            "ok": True,
            "result": {
                "goal": request,
                "action": "edit",
                "reference_objects": [reference_item],
                "required_actions": actions,
                "dependencies": [a["depends_on"] for a in actions],
                "spatial_constraints": spatial,
                "verification_criteria": verification,
                "unsupported_capabilities": [],
                "note": "Ready to edit {0} using only create_part actions.".format(
                    reference_item.get("name")),
            },
        }

    # Fallback: treat as ambiguous.
    return {
        "ok": True,
        "result": {
            "goal": request,
            "action": "ambiguous",
            "reference_objects": [],
            "required_actions": [],
            "dependencies": [],
            "spatial_constraints": [],
            "verification_criteria": [],
            "unsupported_capabilities": [],
            "note": "Could not determine a concrete build or edit intent.",
        },
    }
