# RBXForge — Architecture

> **Status:** Partially implemented. Phase 1 (the minimal local connection between the CLI and
> the Studio plugin) is implemented; the Phase 2 tool layer (`create_part` end-to-end) is
> implemented; the Phase 3A AI provider layer, the Phase 3B single-step agent, and
> the Phase 3C interactive AI REPL (plain text → AI → tool call → Studio) are implemented;
> the Phase 4A basic project inspection (`inspect_hierarchy`), the Phase 4B hierarchy search
> (`find_instances`), the Phase 4C single-instance inspection (`inspect_instance`), the
> **Phase 4D bounded multi-step agent loop** (the model inspects the live project via the
> inspection tools, results are fed back bounded, and it then acts), and the **Phase 4E Groq
> provider** (a second real AI backend via Groq's OpenAI-compatible chat API) are implemented,
> as is the **Phase 7A asset discovery** tool (`asset_search`), the first tool whose execution
> is a read-only **local HTTP** call to the Roblox Open Cloud Creator Store API rather than a
> plugin request, together with the **Phase 7B ranking** tool (`recommend_assets`), which ranks
> the search metadata in-process into bounded, explainable recommendations (no extra API calls),
> and the **Phase 7C insertion** tool (`insert_asset`), which inserts a Creator Store asset into
> the connected project by an id a prior search/ranking returned (never invented), validated on
> the CLI and loaded/placed by the plugin. Phase 7D adds automatic Agent verification after a
> successful `insert_asset` (`inspect_instance` at the reported path, failing closed on mismatch
> or missing instance) and plugin-side reliability checks (parenting verification, bounded unique
> naming, path round-trip validation). Phase 8A adds the `build` orchestration tool for
> scene-aware multi-object construction: the model declares a build plan, inspects the scene,
> executes a bounded sequence of existing action tools, and the Agent verifies every created path
> before reporting `build_failed` or success. Phase 8B adds the `plan_build` orchestration tool:
> the model submits a structured bounded construction plan, the Agent validates and tracks it,
> skips redundant scene inspections, and always reports what was built in plain language. Phase
> 8D adds `edit_build`, `recent_build_context`, and `delete_instance` for iterative build editing:
> the Agent persists a lightweight context of recently-built objects, reads it on follow-up
> requests, applies the smallest set of changes (modify, create, or delete), restricts deletion
> to the recent build context or objects touched in the current edit, and verifies every
> modified/added/removed path before reporting success. Phase 9A adds `analyze_scene`, a bounded,
> deterministic Workspace summary (landmarks, major models, groups, class counts, and optional
> query-relevant objects) that reuses the existing inspection primitives internally but exposes
> only a compact summary to the model. Phase 9B adds `decompose_intent`, a lightweight
> natural-language build/edit intent layer that turns vague requests and the scene summary into
> structured, bounded execution plans containing only actions the current toolset can perform;
> unsupported capabilities are rejected instead of hallucinated. A full verify → fix → report
> cycle remains planned.

## Overview

RBXForge is a layered system. The AI model is **not** the core — the core is the pipeline that
connects a user prompt to changes that actually appear in Roblox Studio.

```
 User prompt
     ↓
┌──────────────────────────┐
│       RBXForge CLI       │   Launch point. Starts the interactive session.
└──────────────────────────┘
     ↓
┌──────────────────────────┐
│     Interactive Agent    │   Understand → Inspect → Plan → Execute → Verify → Fix
└──────────────────────────┘
     ↓
┌──────────────────────────┐
│    AI Provider Layer     │   Ollama (initial) / Groq (hosted) / NIM (optional) / future
└──────────────────────────┘
     ↓
┌──────────────────────────┐
│     RBXForge Tools       │   Roblox-specific tools the agent calls
└──────────────────────────┘
     ↓
┌──────────────────────────┐
│   Local Communication    │   Local WebSocket (implemented, Phase 1)
└──────────────────────────┘
     ↓
┌──────────────────────────┐
│   RBXForge Studio Plugin │   Bridge into Roblox Studio
└──────────────────────────┘
     ↓
┌──────────────────────────┐
│      Roblox Studio       │   The workspace where changes land
└──────────────────────────┘
     ↓
   Verify → Fix → Verify → Report
```

## Planned vs Implemented

| Component | Status |
| --- | --- |
| CLI | **Implemented (Phases 1–4D + Phase 7A/7B/7C/7D + Phase 8A/8B/8D + Phase 9A/9B)** — local WebSocket server, interactive AI REPL (`ping`/`status`/`create_part`/`inspect_hierarchy`/`find_instances`/`inspect_instance`/`asset_search`/`recommend_assets`/`insert_asset`/`build`/`plan_build`/`edit_build`/`recent_build_context`/`delete_instance`/`analyze_scene`/`decompose_intent`/`help`/`quit` + plain text sent to the agent), `create_part` + `inspect_hierarchy` + `find_instances` + `inspect_instance` + `asset_search` + `recommend_assets` + `insert_asset` + `build` + `plan_build` + `edit_build` + `recent_build_context` + `delete_instance` + `analyze_scene` + `decompose_intent` tools ([TOOLS.md](./TOOLS.md)) |
| Interactive agent | **Implemented (bounded, Phase 3B → 4D → 8A/8B/8D/9A/9B)** — `prompt → provider → tool call → ... → action` multi-step loop in `cli/agent.py`: inspection tools feed bounded results back to the model (max 5 tool calls per request, raised to a bounded 12 in build/edit mode); `build` enables multi-object scene-aware construction, `plan_build` validates/tracks structured plans, `edit_build` applies iterative changes with `recent_build_context` and `delete_instance`, `analyze_scene` provides a compact bounded scene summary, and `decompose_intent` turns vague build/edit requests into structured bounded plans before execution; redundant scene inspections are skipped and a natural-language summary is produced; single-step requests preserve the original behavior |
| AI provider layer | **Implemented (Phase 3A + Phase 4E)** — `cli/providers.py`: provider interface, Ollama + Groq + mock backends, env-based config, typed errors ([AI.md](./AI.md)) |
| Agent loop | **Implemented (Phase 4D → 6C → 7D → 8A/8B)** — bounded multi-step loop with project inspection
  and verification. The model inspects the live Roblox project via the inspection
  tools, receives bounded tool results, and then acts through an action tool. After a successful
  `insert_asset`, the Agent automatically calls `inspect_instance` at the reported path and
  reports `verification_failed` if the instance is missing or does not match (Phase 7D). For
  `modify_instance`, the model may call `inspect_instance` once for optional verification; for
  `create_part`/`create_script`, this is permitted only if the model previously called an
  inspection tool during the same request. Phase 8A adds `build` mode: action tools continue until
  a final report, and the Agent verifies every recorded created path before reporting
  `build_failed` or success. Phase 8B adds `plan_build`: the model submits a structured bounded
  plan, the Agent validates and tracks it, caches `inspect_instance` results to skip redundant
  reads, and generates a fallback summary if the final report is empty. Phase 8D adds `edit_build`
  mode: the Agent persists `recent_build_context` across requests, reads it on follow-up edits,
  tracks modified/created/deleted paths, restricts `delete_instance` to recent build objects, and
  verifies every affected path before reporting success. Phase 9A adds `analyze_scene`: the model
  can request a compact bounded summary of the Workspace at the start of complex builds or edits,
  which internally reuses `inspect_hierarchy`/`find_instances` but only exposes the summary. Phase 9B
  adds `decompose_intent`: the model can turn a vague natural-language build/edit request and the
  scene summary into a structured bounded plan, then feed it to `build`/`plan_build` or
  `edit_build`; unsupported or ambiguous requests are rejected instead of hallucinated.
  Verification is skipped when the tool
  result already provides sufficient information. Single-step requests preserve the original
  behavior: one model call → one tool call → done (unless inspection context was gathered). The
  full understand/plan/verify/fix autonomy is still planned. |
| Tool system | **Partially implemented (Phase 2B + Phase 4A + Phase 4B + Phase 4C + Phase 6A + Phase 6B + Phase 7A + Phase 7B + Phase 7C + Phase 7D + Phase 8A/8B/8D + Phase 9A/9B)** — `create_part` (Phase 2B), `inspect_hierarchy` (Phase 4A), `find_instances` (Phase 4B), `inspect_instance` (Phase 4C), `create_script` (Phase 6A), `modify_instance` (Phase 6B), `insert_asset` (Phase 7C/7D), `build` (Phase 8A), `plan_build` (Phase 8B), `edit_build` / `recent_build_context` / `delete_instance` (Phase 8D), `analyze_scene` (Phase 9A), and `decompose_intent` (Phase 9B) live end-to-end or in the Agent loop; `asset_search` (Phase 7A) and `recommend_assets` (Phase 7B) are schema-registered alongside them but execute as local HTTP calls (no plugin handler); more tools planned |
| Project inspection / index | **Started (Phase 4A + Phase 4B + Phase 4C + Phase 4D + Phase 9A/9B)** — `inspect_hierarchy` snapshots the Workspace tree (bounded, Name/ClassName); `find_instances` searches the live Workspace by name (bounded, case-insensitive, with full paths); `inspect_instance` reads one instance by full path with an allowlisted safe-property set; the Phase 4D agent loop drives these live before acting; Phase 9A adds `analyze_scene`, a higher-level bounded summary that reuses these inspection primitives; Phase 9B adds `decompose_intent`, which consumes that summary to produce structured, bounded build/edit plans; indexing/temporal tracking still planned |
| Local communication layer | **Implemented (Phases 1–2B)** — local WebSocket transport, ping/pong, tool requests/responses, see [PROTOCOL.md](./PROTOCOL.md) |
| Studio plugin | **Implemented (Phases 1–2B + Phase 4A + Phase 4B + Phase 4C + Phase 6A + Phase 6B + Phase 7C + Phase 7D + Phase 8D)** — connects to RBXForge, answers ping/pong, executes `create_part`, `inspect_hierarchy`, `find_instances`, `inspect_instance`, `create_script`, `modify_instance`, `insert_asset`, and `delete_instance`; `insert_asset` includes Phase 7D reliability checks (parenting verification, bounded unique naming, path round-trip), see [PLUGIN.md](./PLUGIN.md) |
| Verification system | **Implemented (Phase 6C + Phase 7D + Phase 8A/8B/8D)** — optional model-driven verification after action tool
  success for `modify_instance` and conditionally for `create_part`/`create_script`; mandatory
  automatic Agent verification for `insert_asset` (Phase 7D), for every path created during a
  `build` (Phase 8A), and for every modified/created/deleted path during an `edit_build` (Phase
  8D). After a successful `insert_asset`, the Agent executes one `inspect_instance` call at the
  reported path and reports `verification_failed` if the instance is missing or its path/name/class
  does not match. After a `build` final report, the Agent executes one `inspect_instance` call per
  recorded created path and reports `build_failed` if any step failed or any path cannot be
  confirmed; during build mode successful inspections are cached so redundant reads of the same
  path are skipped. After an `edit_build` final report, the Agent verifies modified/created paths
  still exist and deleted paths are gone. After a successful `modify_instance`, one optional
  `inspect_instance` verification step is permitted. After a successful `create_part`/`create_script`,
  one optional step is permitted only if the model previously called an inspection tool during the
  same request. Verification is skipped when the tool result already provides sufficient
  information. The system distinguishes between the mutation success and independently verified
  properties. See [AI.md](./AI.md) for the full verification behavior specification.

Implemented today: the Phase 1 local connection, the Phase 2 tool layer (create_part), the
Phase 3A AI provider layer, the Phase 3B single-step agent, the Phase 3C interactive
AI REPL, the Phase 4A basic project inspection (inspect_hierarchy), the Phase 4B hierarchy
search (find_instances), the Phase 4C single-instance inspection (inspect_instance), the
Phase 4D bounded multi-step agent loop (the model inspects the live project, receives bounded
tool results, and then acts through an action tool), and the Phase 4E Groq provider (hosted
models via Groq's OpenAI-compatible chat API). A full verification system and a complete
plan → verify → fix cycle are still planned.

## Components

### CLI

The command-line entry point. The user runs `rbxforge` and enters an interactive session.

```
$ rbxforge
RBXForge > create a small medieval shop
```

Responsibilities (planned):

- Start and manage the interactive session.
- Route user input to the agent.
- Display agent output and reports.

### Interactive Agent

The orchestration brain. It receives a user prompt and drives the full loop:

1. **Understand** the request.
2. **Inspect** relevant project context.
3. **Plan** the change.
4. **Execute** using RBXForge tools.
5. **Verify** the result in Studio.
6. **Fix** if necessary, then **verify again**.
7. **Report** what changed.

**Phase 3B (implemented, single step):** `cli/agent.py` provides a single-step slice of this —
`prompt → provider → structured tool call → ToolRegistry execution → result`. It gives the model
the currently registered tool definitions, parses the model's JSON tool call, and executes it
through the ToolRegistry only, rejecting unknown tools / invalid arguments safely.

**Phase 3C (implemented):** the interactive REPL feeds `Agent.run` with any non-command input
(`RBXForge> create a red cube` → AI → tool call → Studio), plus an explicit `ask` command, and
logs one concise line per run.

**Phase 4D (implemented, bounded multi-step loop):** `Agent.run` now drives a short loop. The
model may call the inspection tools (`find_instances` / `inspect_instance` / `inspect_hierarchy`)
to gather live project context; each successful tool result is appended to the conversation as a
**bounded, compacted** payload; the model can then call another inspection tool or an action tool
(`create_part`). The loop stops after an action tool succeeds, on a final model report, on any
hard rejection (`unknown_tool` / `invalid_arguments` / `malformed_output` / `provider_error` /
`execution_failed`), or after **5 executed tool calls** (`max_tool_calls`). Every call still goes
through the `ToolRegistry`; no tool, validation, or plugin/protocol behavior changed. Simple
requests behave exactly as under Phase 3B. A full plan → verify → fix autonomy is not implemented
yet.

See [AGENT.md](./AGENT.md) for the full expected behavior.

### AI Provider Layer

An abstraction over the underlying model, so the agent does not care which provider backs it.

- **Initial preferred backend:** Ollama (local models).
- **Implemented hosted backend:** Groq (`RBXFORGE_PROVIDER=groq`) — Groq's OpenAI-compatible
  chat API with `RBXFORGE_API_KEY`, same `Provider` interface and JSON-in-text tool calling as
  Ollama, so the agent loop is unchanged (Phase 4E).
- **Optional backend:** NVIDIA NIM (recognized placeholder, not implemented).
- **Future:** additional providers without rewriting the agent.

See [AI.md](./AI.md).

### Agent Loop

RBXForge should eventually operate as a full agent loop:

```
PROMPT
   ↓
UNDERSTAND
   ↓
INSPECT
   ↓
PLAN
   ↓
EXECUTE
   ↓
VERIFY
   ↓
SUCCESS?
 ┌─┴─┐
YES  NO
 ↓    ↓
DONE  DIAGNOSE
       ↓
      FIX
       ↓
     VERIFY
```

The goal is not merely to generate code. The goal is to **make the requested change actually
work in Roblox Studio**.

**Implemented (Phase 4D):** a bounded slice of this loop — the model inspects the live project
via the inspection tools, receives bounded tool results, and then acts through an action tool
(see [AI.md](./AI.md)). The full understand/plan/verify/fix autonomy is still planned.

### Tool System

The AI does **not** manipulate Roblox arbitrarily. Instead, RBXForge exposes Roblox-specific
tools. The agent selects and calls these tools.

Conceptual tool examples (planned):

- `inspect_project`
- `search_instances`
- `create_part`
- `create_model`
- `create_folder`
- `modify_instance`
- `move_object`
- `rotate_object`
- `scale_object`
- `create_ui`
- `delete_instance`
- `create_script`
- `run_luau`
- `verify`

See [TOOLS.md](./TOOLS.md). `create_part` is implemented end-to-end (Phase 2B),
`inspect_hierarchy` (Phase 4A) snapshots the current `workspace` instance tree as a bounded
Name/ClassName structure — the first read-only "inspection" tool and the concrete start of the
Project Inspection component — `find_instances` (Phase 4B) searches the live `workspace` by
instance name (case-insensitive substring match) and returns each match's Name, ClassName, and
full Instance path, bounded by `max_results`, and `inspect_instance` (Phase 4C) reads one
instance by its full path and returns its identity, full path, parent path, and an allowlisted
safe-property set. `create_script` (Phase 6A) creates a Script/LocalScript/ModuleScript with
optional Luau source at a game-rooted parent path (or the per-type default container).
`modify_instance` (Phase 6B) changes a small allowlisted property set on one live instance.
`asset_search` (Phase 7A) searches the public Roblox Creator Store over the Open Cloud API —
a read-only local HTTP call that is deliberately **not** a Studio operation.
`recommend_assets` (Phase 7B) ranks those results in-process into a bounded, explainable
recommendation list (no extra API calls; still read-only). `insert_asset` (Phase 7C) inserts
a Creator Store asset into the project: the CLI only accepts an `asset_id` that a prior
search/ranking returned (bounded known-id registry — ids are never invented) and the plugin
loads, uniquely names, parents, and positions it (explicit position / near a reference / beside
the SpawnLocation). The remaining conceptual tools are
not implemented.

### Project Inspection / Index

RBXForge should understand the existing Roblox project rather than blindly creating duplicate
systems. Long-term, this uses intelligent project inspection and indexing.

- **Phase 4A (implemented):** `inspect_hierarchy` returns a bounded tree of the current
  `workspace` — every node is `{ name, className, children }`, the depth is configurable
  (default 3, max 50), and truncation is flagged rather than serializing everything. This gives
  the agent a small, structured view of what exists before it acts.
- **Phase 4B (implemented):** `find_instances` searches the **live** Workspace hierarchy by
  instance name (case-insensitive substring match) and returns a bounded list of `{ name,
  className, path }` matches (default 20, max 100) with a total match count and a truncation
  flag. It reads the hierarchy on every request — deliberately **no** caching or indexing yet.
- **Phase 4C (implemented):** `inspect_instance` resolves one instance by its full path and
  returns its identity, full path, parent path, and a small **allowlisted** set of safe
  properties (BasePart/SpawnLocation/Model/GuiObject; see [TOOLS.md](./TOOLS.md)). Non-goals:
  no arbitrary property reflection, no recursive descendant inspection, no caching — each path
  is resolved live.
- **Phase 4D (implemented):** the **bounded multi-step agent loop** now puts the inspection tools
  to work. Before executing an action tool, the agent may call `find_instances` /
  `inspect_instance` / `inspect_hierarchy` to gather live project context; **bounded, compacted
  tool results are returned to the model** (capped lists, truncated strings, a hard serialized
  character budget), and the model then acts. This is the concrete start of "inspect before
  acting": it works against the live Workspace on every request, is capped at **5 tool calls per
  request**, and never dumps unbounded hierarchy or property data into the prompt.
- Yet still planned: indexing, spatial reasoning, temporal tracking, arbitrary property
  serialization, and persistent / selective project-context loading.

Example:

```
User: add a shop near the town
```

RBXForge should be able to:

1. Find "Town".
2. Find relevant existing systems.
3. Inspect nearby objects.
4. Check existing shop / currency / inventory systems.
5. Plan around the existing architecture.

It should load only relevant project context into the AI whenever possible. The full index and
generalized search (future class-type / property search, `get_instance`) build on the Phase 4A
snapshot and the Phase 4B name search.

### Local Communication Layer

The bridge between RBXForge and the Studio plugin.

- **Approach (implemented):** a local WebSocket connection (`ws://127.0.0.1:7676` by default).
  RBXForge runs the WebSocket server; the plugin is the client.
- **Protocol:** implemented message set is documented in [PROTOCOL.md](./PROTOCOL.md)
  (`hello`, `welcome`, `ping`, `pong`, `bye`, `error`).
- No network dependency is required to be deployed anywhere; communication is local to the
  developer's machine.

### RBXForge Studio Plugin

The plugin is the only component that touches Roblox Studio. It receives requests from RBXForge,
performs the corresponding Studio operations, and returns results.

- **Phase 1 (implemented):** a minimal plugin that connects to the local RBXForge process over
  WebSocket, announces itself with `hello`, and answers `ping` with `pong`.
- **Phase 2 (implemented):** Studio operations for `create_part` (creates a part and returns ok
  or an error).
- **Phase 4A (implemented):** Studio operation for `inspect_hierarchy` (walks `workspace`,
  returning a bounded Name/ClassName tree honoring a depth limit and flagging truncation).
- **Phase 4B (implemented):** Studio operation for `find_instances` (searches the live
  `workspace` by instance name — case-insensitive substring match — returning a bounded list of
  `{ name, className, path }` matches with a total count and truncation flag).
- **Phase 4C (implemented):** Studio operation for `inspect_instance` (resolves a full path
  from `workspace`, validating the path format strictly, and returns the instance's identity,
  full path, parent path, and an allowlisted safe-property set; `not_found` when the path does
  not resolve).

See [PLUGIN.md](./PLUGIN.md).

### Roblox Studio

The workspace. The plugin operates on the currently open project.

### Future Verification System

A planned capability that confirms a change actually worked — e.g. an object exists with the
right properties, a script runs without errors. Verification is part of the agent loop and will
be built over time. It is not implemented.

## Data / Request Flow (Conceptual)

```
 User prompt
   ↓
 CLI / Agent (bounded multi-step loop, Phase 4D)
   ↓
 AI Provider (model produces one JSON object per step: tool call or final report)
   ↓
  Tool system (every call validated + executed through the ToolRegistry)
   ↓
  request message
   ↓
 Local communication (WebSocket, implemented for Phase 1)
   ↓
 Studio plugin executes in Roblox Studio
   ↓
 Response message returns (bounded, compacted result fed back to the model)
   ↓
  …inspection tools gather live context before the action tool…
   ↓
 agent reports concisely (AgentResult); full verify/fix/report is planned
```

## Design Principles

1. **The AI model is interchangeable.** Providers plug in behind a stable abstraction.
2. **Roblox-specific tools, not arbitrary mutation.** The tool system constrains what the AI can do.
3. **The plugin is the only bridge into Studio.** No other component touches Studio directly.
4. **Project-aware.** Inspect before acting; avoid duplicate systems.
5. **Verification first.** A change is not done until it is verified.
6. **Small, verifiable milestones.** See [ROADMAP.md](./ROADMAP.md) and
   [DEVELOPMENT.md](./DEVELOPMENT.md).
