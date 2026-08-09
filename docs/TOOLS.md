# RBXForge — Tool System (Conceptual)

> **Status:** Planned, with one implemented prototype.
>
> - **Implemented (Phase 2A):** `create_part` — a minimal, hard-coded prototype that creates a
>   single Part in `workspace`. It is wired end-to-end (CLI → `request` message → plugin →
>   Studio → `response`) but takes only fixed test parameters. See
>   [PROTOCOL.md](./PROTOCOL.md) for the wire format.
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

## Tool Anatomy

Each tool is described conceptually by:

- **Purpose:** What the tool is for.
- **Inputs (conceptual):** The kind of arguments the tool needs, described functionally rather
  than as a final schema.
- **Expected output:** What the agent can expect back (success/failure, plus data).
- **Why the agent might use it:** Typical situations where the tool is the right choice.

---

### inspect_project

- **Purpose:** Get a structural overview of the currently open Roblox project so the agent
  understands what exists before acting.
- **Inputs (conceptual):** Optional filter/scope (e.g. top-level only, a subtree).
- **Expected output:** A tree or summary of instances, folders, and scripts.
- **Why the agent might use it:** Starting a task that touches an unknown project; choosing where
  to place a new object.

### search_instances

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
- Some tools overlap with what the plugin natively does; the tool layer adds agent-facing
  semantics on top.
- Verification should not necessarily be a separate tool call — it may be folded into tool
  results. This is an open design question.
- The tool set will grow with the roadmap (e.g. gameplay-specific tools in Phase 6).
