# RBXForge — Tool System

> **Status:** Three tools implemented (create_part in Phase 2B, inspect_hierarchy in Phase 4A,
> find_instances in Phase 4B); the rest is conceptual.
>
> - **Implemented (Phase 2B):** `create_part` is the first **formal RBXForge tool**. It is
>   registered in a tool registry on the CLI side (`cli/rbxforge.py`) with metadata — **name,
>   description, input schema** — and arguments are **validated against that schema before any
>   request is sent**. The Studio plugin (`plugin/rbxforge.lua`) dispatches incoming `request`
>   messages through its own tool-handler registry. See [PROTOCOL.md](./PROTOCOL.md) for the wire
>   format.
> - **Implemented (Phase 4A):** `inspect_hierarchy` snapshots the Workspace instance tree
>   (Name + ClassName per node), bounded by a configurable depth. It uses the same registry,
>   validation, and request/response flow as `create_part`.
> - **Implemented (Phase 4B):** `find_instances` searches the live Workspace hierarchy by
>   instance name (case-insensitive substring match) and returns each match's Name, ClassName,
>   and full Instance path, bounded by `max_results`. Same registry/validation/protocol flow.
> - **Planned:** everything else below is conceptual only. **No other tools are implemented.**
>   Final APIs are deliberately **not invented yet.**

## Purpose

The tool system is the interface between the RBXForge agent and Roblox Studio. The AI does
**not** manipulate Roblox arbitrarily. Instead, it selects and calls RBXForge tools. The tool
system constrains what the agent can do, makes operations verifiable, and is the foundation of
RBXForge (see decision D-011 in [DECISIONS.md](./DECISIONS.md)).

```
AI Model
   ↓
Agent  ── calls ──►  RBXForge Tool System  ──►  Studio Plugin  ──►  Roblox Studio
```

## Implemented Tools

### create_part

- **Purpose:** Create a Part in the current project's `workspace`.
- **Inputs (schema, validated by the CLI before sending):**
  - `name` — string, at least one character
  - `position` — object with numeric `x`, `y`, `z`
  - `size` — object with numeric `x`, `y`, `z`
  - `color` — string, one of `"red"`
- **Expected output:** `ok: true` with the created part's `{ name, position, size, color }`,
  or `ok: false` with `error.code` / `error.message`.
- **Why the agent might use it:** "create a red cube" (prototype for the future, general
  `create_instance` tool).

The CLI exposes one command, `create_part`, which runs this tool with its fixed test defaults.
The tool layer is generic: any argument set that passes the input schema can be executed via the
registry (`ToolRegistry.execute`).

### inspect_hierarchy (Phase 4A)

- **Purpose:** Snapshot the current `workspace` instance tree so the agent can see what exists
  before acting. This is the first **inspection** (read-only) tool; everything before it modified
  the project.
- **Inputs (schema, validated by the CLI before sending):**
  - `depth` — optional whole number (`1..50`, default `3`). Bound on how many levels of
    children are serialized.
- **Expected output:** `ok: true` with a `result` containing:
  - `root` — `"Workspace"`
  - `depth` — the depth actually used (default filled in when omitted)
  - `count` — total number of instance nodes serialized (bounded)
  - `truncated` — `true` if any node had children that were omitted by the depth limit
  - `tree` — array with one node: `{ name, className, children: [ ... ] }`
  Each node carries only **Name and ClassName** (deliberately minimal; property serialization is
  out of scope). Leaf nodes have `children: []`; when the limit is hit, the omitted children are
  not serialized and `truncated` is set instead.
- **Why the agent might use it:** orienting before creating/moving objects; checking whether an
  expected instance exists (the future `search_instances` / `get_instance` tools build on this).

The CLI exposes `inspect_hierarchy [depth]` as a REPL command and
`--inspect-hierarchy-once [--depth N]` as a one-shot flag. Depth is validated to be a whole
number in `1..50`; anything else is rejected before a request is sent.

### find_instances (Phase 4B)

- **Purpose:** Search the live Workspace hierarchy by instance name and return the matching
  instances with their full Instance paths. This is the first real **search** tool (the
  Phase 4A snapshot plus this query together form the practical start of Project Awareness);
  it reads the hierarchy on every request — no caching or indexing yet.
- **Inputs (schema, validated by the CLI before sending):**
  - `query` — required string, at least one character. Matched against instance **Name**
    case-insensitively (substring match: "baseplate", "BasePlate", and "plate" all find
    `Baseplate`).
  - `max_results` — optional whole number (`1..100`, default `20`). Bounds how many matches
    are returned so the response stays bounded on large projects.
- **Expected output:** `ok: true` with a `result` containing:
  - `query` — the query as sent
  - `max_results` — the result cap actually used (default filled in when omitted)
  - `total` — total number of matches found in the live hierarchy
  - `count` — number of matches returned (`min(total, max_results)`)
  - `truncated` — `true` when more matches exist than were returned (`total > count`)
  - `matches` — array of `{ name, className, path }`. `path` is the full Instance path from
    Workspace down, e.g. `"Workspace/Shop/Door"`. Deliberately minimal (Name, ClassName, path
    only — no arbitrary property inspection).
- **Why the agent might use it:** locating "Town", an existing shop, or a folder to extend
  before acting; answering "what exists in the project" (Phase 4 goal).

The CLI exposes `find_instances <query> [max_results]` as a REPL command (a trailing integer
token is parsed as `max_results`) and `--find-instances-once --query <text> [--max-results N]`
as a one-shot flag. The query must be a non-empty string and `max_results` a whole number in
`1..100`; anything else is rejected before a request is sent.

## Conceptual Tool List

This is a starting list, not a final API. Tools will be added, removed, and refined during
implementation.

| Tool | Category | Purpose (conceptual) |
| --- | --- | --- |
| `inspect_project` | Project awareness | Get an overview of the project structure |
| `search_instances` | Project awareness | Find instances by name, type, or property |
| `get_instance` | Project awareness | Read details of a specific instance |
| `create_instance` | Modify | Create a new instance (part, model, folder, script, etc.) |
| `modify_instance` | Modify | Change properties of an existing instance |
| `delete_instance` | Modify | Remove an instance |
| `move_instance` | Modify | Change position / parent |
| `rotate_instance` | Modify | Change orientation |
| `scale_instance` | Modify | Change size / scale |
| `create_script` | Gameplay logic | Create a Luau script |
| `modify_script` | Gameplay logic | Edit a Luau script's source |
| `create_ui` | UI | Create UI elements |
| `run_luau` | Execution | Run a Luau snippet in the project context |
| `verify` | Verification | Confirm a change exists and matches intent |

These names match the conceptual set in the product vision. Some vision examples were worded as
`create_part`, `create_model`, `create_folder`; here they are grouped under `create_instance`
with a `type` parameter. The exact granularity is **not decided** and is an open design question.
`create_part` is implemented (Phase 2B) as the first concrete tool and serves as the template for
the future `create_instance`.

## Tool Anatomy

Each tool is described conceptually by:

- **Purpose:** What the tool is for.
- **Inputs (conceptual):** The kind of arguments the tool needs, described functionally rather
  than as a final schema.
- **Expected output:** What the agent can expect back (success/failure, plus data).
- **Why the agent might use it:** Typical situations where the tool is the right choice.

> **Implemented tool anatomy (Phase 2B/4A/4B):** every registered tool carries machine-readable
> metadata — `name`, `description`, and an `input_schema` — and the CLI validates arguments
> against that schema before sending a `request`. `create_part` (Phase 2B), `inspect_hierarchy`
> (Phase 4A), and `find_instances` (Phase 4B) are the implemented tools; the conceptual entries
> below are still being designed.

---

### inspect_project

- **Purpose:** Get a structural overview of the currently open Roblox project so the agent
  understands what exists before acting.
- **Inputs (conceptual):** Optional filter/scope (e.g. top-level only, a subtree).
- **Expected output:** A tree or summary of instances, folders, and scripts.
- **Why the agent might use it:** Starting a task that touches an unknown project; choosing where
  to place a new object.

### search_instances

> **Note:** the name-search subset of this idea is now **implemented as `find_instances`
> (Phase 4B)**. This entry remains conceptual for the full version (search by class type /
> property value, parent scope).

- **Purpose:** Find specific instances by name, class type, or property value.
- **Inputs (conceptual):** Search query (name/type/property), optional parent scope.
- **Expected output:** Matching instances (or "none found").
- **Why the agent might use it:** Locating "Town", an existing shop, or a folder to extend.

### get_instance

- **Purpose:** Read full details of a single instance.
- **Inputs (conceptual):** Reference to the instance.
- **Expected output:** The instance's properties, children, and location in the tree.
- **Why the agent might use it:** Inspecting a specific object before modifying it.

### create_instance

- **Purpose:** Create a new instance (Part, Model, Folder, etc.) in the project.
- **Inputs (conceptual):** Class type, name, parent, initial properties.
- **Expected output:** Success/failure plus the created instance's reference.
- **Why the agent might use it:** "create a red cube", "create a folder", "create a model".

### modify_instance

- **Purpose:** Change properties of an existing instance.
- **Inputs (conceptual):** Instance reference, property/value pairs to set.
- **Expected output:** Success/failure plus confirmation of the new property values.
- **Why the agent might use it:** "make the cube red", "rename the model".

### delete_instance

- **Purpose:** Remove an instance from the project.
- **Inputs (conceptual):** Instance reference.
- **Expected output:** Success/failure.
- **Why the agent might use it:** Removing objects the user asked to remove.
- **Safety note:** Destructive; safe-deletion/undo semantics are an open decision (U-001).

### move_instance

- **Purpose:** Change an instance's position and/or parent.
- **Inputs (conceptual):** Instance reference, target position, optional new parent.
- **Expected output:** Success/failure plus the new position/parent.
- **Why the agent might use it:** "move the cube forward", "put the shop near the town".

### rotate_instance

- **Purpose:** Change an instance's orientation.
- **Inputs (conceptual):** Instance reference, rotation/angles.
- **Expected output:** Success/failure plus the new orientation.
- **Why the agent might use it:** Angling a sign, wall, or door.

### scale_instance

- **Purpose:** Change an instance's size/scale.
- **Inputs (conceptual):** Instance reference, size/scale values.
- **Expected output:** Success/failure plus the new size.
- **Why the agent might use it:** "make the cube bigger".

### create_script

- **Purpose:** Create a Luau script (Script, LocalScript, ModuleScript) in the project.
- **Inputs (conceptual):** Script type, name, parent, source content.
- **Expected output:** Success/failure plus the script's reference.
- **Why the agent might use it:** Adding gameplay logic (Phase 6).

### modify_script

- **Purpose:** Edit an existing Luau script's source.
- **Inputs (conceptual):** Script reference, new source (or a patch).
- **Expected output:** Success/failure.
- **Why the agent might use it:** Fixing or extending gameplay logic.

### create_ui

- **Purpose:** Create UI elements (ScreenGui, Frames, TextLabels, etc.).
- **Inputs (conceptual):** UI hierarchy and properties.
- **Expected output:** Success/failure plus the created UI's reference.
- **Why the agent might use it:** "add UI to show the player's gold".

### run_luau

- **Purpose:** Run a Luau snippet in the project context (in Studio, via the plugin).
- **Inputs (conceptual):** Luau source, optional execution scope.
- **Expected output:** Results/return values, or errors.
- **Why the agent might use it:** Computations, queries, and advanced verification that the
  structured tools do not cover.
- **Safety note:** Powerful and potentially dangerous; requires careful sandboxing/guardrails
  before implementation.

### verify

- **Purpose:** Confirm that a change actually exists and matches intent.
- **Inputs (conceptual):** What to check (instance exists, property value, script runs).
- **Expected output:** Pass/fail with details.
- **Why the agent might use it:** Every executed change should be verified (see
  [AGENT.md](./AGENT.md)). What "verified" means per tool is an open decision (U-005).

## Design Notes

- The list above is **conceptual**. Exact names, arguments, and schemas are open questions.
- **Implemented registry (Phase 2B/4A/4B):** the CLI keeps a `ToolRegistry` keyed by tool name;
  each `Tool` carries `name`, `description`, and `input_schema` and knows how to turn validated
  arguments into a protocol request/response exchange. Adding a tool later means registering one
  more `Tool` — no protocol changes required. `inspect_hierarchy` (Phase 4A) and `find_instances`
  (Phase 4B) demonstrate this: each shipped with no protocol changes, only a new client-side
  `Tool` and a matching plugin-side handler.
- Some tools overlap with what the plugin natively does; the tool layer adds agent-facing
  semantics on top.
- Verification should not necessarily be a separate tool call — it may be folded into tool
  results. This is an open design question.
- The tool set will grow with the roadmap (e.g. gameplay-specific tools in Phase 6).
