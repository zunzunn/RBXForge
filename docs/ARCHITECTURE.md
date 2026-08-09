# RBXForge — Architecture

> **Status:** Partially implemented. Phase 1 (the minimal local connection between the CLI and
> the Studio plugin) is implemented; everything else remains planned.

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
│    AI Provider Layer     │   Ollama (initial) / NVIDIA NIM (optional) / future providers
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
| CLI | **Implemented (Phase 1, minimal)** — starts the local WebSocket server and a command prompt (`ping`, `status`, `quit`) |
| Interactive agent | **Planned** — not implemented |
| AI provider layer | **Planned** — not implemented |
| Agent loop | **Planned** — not implemented |
| Tool system | **Planned** — conceptual only |
| Project inspection / index | **Planned** — long-term |
| Local communication layer | **Implemented (Phase 1, minimal)** — local WebSocket transport + ping/pong, see [PROTOCOL.md](./PROTOCOL.md) |
| Studio plugin | **Implemented (Phase 1, minimal)** — connects to RBXForge and answers ping/pong, see [PLUGIN.md](./PLUGIN.md) |
| Verification system | **Planned** — future |

At this point, **only the Phase 1 local connection exists.** The AI agent, tool system, and
everything else are still planned.

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

See [AGENT.md](./AGENT.md) for the full expected behavior.

### AI Provider Layer

An abstraction over the underlying model, so the agent does not care which provider backs it.

- **Initial preferred backend:** Ollama (local models).
- **Optional backend:** NVIDIA NIM.
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

See [TOOLS.md](./TOOLS.md). Tools are conceptual at this stage; no final APIs exist.

### Project Inspection / Index

RBXForge should understand the existing Roblox project rather than blindly creating duplicate
systems. Long-term, this uses intelligent project inspection and indexing.

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

It should load only relevant project context into the AI whenever possible.

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
- **Phase 2+ (planned):** Studio operations (create/modify instances, etc.).

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
 CLI / Agent  ──►  AI Provider (model produces tool calls)
   ↓
  Tool system  ──►  request message
    ↓
  Local communication (WebSocket, implemented for Phase 1)
   ↓
 Studio plugin executes in Roblox Studio
   ↓
 Response message returns
   ↓
 Agent verifies → fixes if needed → reports
```

## Design Principles

1. **The AI model is interchangeable.** Providers plug in behind a stable abstraction.
2. **Roblox-specific tools, not arbitrary mutation.** The tool system constrains what the AI can do.
3. **The plugin is the only bridge into Studio.** No other component touches Studio directly.
4. **Project-aware.** Inspect before acting; avoid duplicate systems.
5. **Verification first.** A change is not done until it is verified.
6. **Small, verifiable milestones.** See [ROADMAP.md](./ROADMAP.md) and
   [DEVELOPMENT.md](./DEVELOPMENT.md).
