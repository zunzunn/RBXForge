# RBXForge — Architecture

> **Status:** Partially implemented. Phase 1 (the minimal local connection between the CLI and
> the Studio plugin) is implemented; the Phase 2 tool layer (`create_part` end-to-end) is
> implemented; the Phase 3A AI provider layer, the Phase 3B single-step agent, and
> the Phase 3C interactive AI REPL (plain text → AI → tool call → Studio) are implemented;
> the Phase 4A basic project inspection (`inspect_hierarchy`), the Phase 4B hierarchy search
> (`find_instances`), the Phase 4C single-instance inspection (`inspect_instance`), the
> **Phase 4D bounded multi-step agent loop** (the model inspects the live project via the
> inspection tools, results are fed back bounded, and it then acts), and the **Phase 4E Groq
> provider** (a second real AI backend via Groq's OpenAI-compatible chat API) are implemented.
> A full verify → fix → report cycle remains planned.

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
| CLI | **Implemented (Phases 1–4D)** — local WebSocket server, interactive AI REPL (`ping`/`status`/`create_part`/`inspect_hierarchy`/`find_instances`/`inspect_instance`/`help`/`quit` + plain text sent to the agent), `create_part` + `inspect_hierarchy` + `find_instances` + `inspect_instance` tools ([TOOLS.md](./TOOLS.md)) |
| Interactive agent | **Implemented (bounded, Phase 3B → 4D)** — `prompt → provider → tool call → ... → action` multi-step loop in `cli/agent.py`: inspection tools feed bounded results back to the model (max 5 tool calls per request); single-step requests preserve the original behavior |
| AI provider layer | **Implemented (Phase 3A + Phase 4E)** — `cli/providers.py`: provider interface, Ollama + Groq + mock backends, env-based config, typed errors ([AI.md](./AI.md)) |
| Agent loop | **Partially implemented (Phase 4D)** — a bounded multi-step loop (inspect → act) is done; the full understand/plan/verify/fix cycle is planned |
| Tool system | **Partially implemented (Phase 2B + Phase 4A + Phase 4B + Phase 4C)** — `create_part` (Phase 2B), `inspect_hierarchy` (Phase 4A), `find_instances` (Phase 4B), and `inspect_instance` (Phase 4C) live end-to-end (CLI + plugin); more tools planned |
| Project inspection / index | **Started (Phase 4A + Phase 4B + Phase 4C + Phase 4D)** — `inspect_hierarchy` snapshots the Workspace tree (bounded, Name/ClassName); `find_instances` searches the live Workspace by name (bounded, case-insensitive, with full paths); `inspect_instance` reads one instance by full path with an allowlisted safe-property set; the Phase 4D agent loop drives these live before acting; indexing/temporal tracking still planned |
| Local communication layer | **Implemented (Phases 1–2B)** — local WebSocket transport, ping/pong, tool requests/responses, see [PROTOCOL.md](./PROTOCOL.md) |
| Studio plugin | **Implemented (Phases 1–2B + Phase 4A + Phase 4B + Phase 4C)** — connects to RBXForge, answers ping/pong, executes `create_part`, `inspect_hierarchy`, `find_instances`, and `inspect_instance`, see [PLUGIN.md](./PLUGIN.md) |
| Verification system | **Planned** — future |

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
safe-property set. The remaining conceptual tools are not implemented.

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
