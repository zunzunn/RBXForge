# RBXForge — Project

> **Status:** Documentation only. No implementation exists yet.
>
> **Scope:** This document is the high-level source of truth for RBXForge. It is intentionally
> architectural and product-focused. Implementation details live in the other `/docs` files.

## What RBXForge Is

RBXForge is an AI-powered command-line tool and agent for building and modifying Roblox games
directly inside an open Roblox Studio project.

The user runs a single command:

```
rbxforge
```

and enters an interactive AI session:

```
RBXForge > add a shop
RBXForge > make it medieval
RBXForge > add five swords
```

RBXForge understands the user's existing Roblox project and modifies the currently open Roblox
Studio project — creating, modifying, and verifying real Studio objects.

## Product Vision

The user launches `rbxforge` and interacts with an AI agent that treats Roblox Studio as the
workspace. Changes made by the agent actually appear in Studio, and the agent verifies its own
work.

```
User prompt
    ↓
RBXForge AI Agent
    ↓
Inspect relevant existing project
    ↓
Plan
    ↓
Use RBXForge tools
    ↓
RBXForge Studio Plugin
    ↓
Roblox Studio
    ↓
Verify result
    ↓
Fix if necessary
    ↓
Report completion
```

The long-term product should eventually be capable of building complete gameplay features,
including:

- 3D objects / models
- maps
- UI
- NPCs
- gameplay systems
- Luau scripts
- RemoteEvents / RemoteFunctions
- data systems
- shops
- inventories
- combat
- quests
- and more

However, the **first version** focuses on building and modifying Studio objects (Instances).
Gameplay logic is planned for later phases.

## Target User

Roblox developers who:

- Want to build or modify their games faster using natural-language prompts.
- Work primarily in Roblox Studio.
- Are comfortable with a CLI tool but do not want to hand-write every Instance and script.

## Core Use Case

The core use case is a single interactive session where the user describes what they want, and
RBXForge performs the change in the currently open Roblox Studio project:

```
$ rbxforge
RBXForge > create a small medieval shop
RBXForge >
```

RBXForge executes changes automatically. It does **not** ask for confirmation for every normal
operation.

## Example User Interactions

```
RBXForge > create a red cube
RBXForge > make the cube bigger
RBXForge > move the cube forward
RBXForge > create five swords near the cube
RBXForge > build a small medieval shop
RBXForge > add a shop near the town
RBXForge > add UI to show the player's gold
RBXForge > add an inventory system
```

## Long-Term Goal

RBXForge becomes a reliable autonomous agent that can:

1. Understand an existing Roblox project.
2. Plan changes that fit the existing architecture instead of duplicating systems.
3. Execute changes through a safe, Roblox-specific tool system.
4. Verify changes actually work in Roblox Studio.
5. Diagnose and fix failures.
6. Report exactly what changed.

The goal is not merely to generate code. The goal is to **make the requested change actually
work in Roblox Studio**.

## Current Development Stage

| Stage | Status |
| --- | --- |
| Project definition and documentation | **In progress (this step)** |
| CLI | Planned — not implemented |
| AI agent | Planned — not implemented |
| Studio plugin | Planned — not implemented |
| Communication protocol | Draft — not implemented |
| Tool system | Planned — not implemented |

At this stage, **no implementation code exists**. Everything described in this documentation is
either *current* (the project definition itself), *planned*, or *future*.

## What RBXForge Is NOT

- **Not** a general-purpose code generator. RBXForge is Roblox-specific and works through the
  Studio plugin.
- **Not** a replacement for Roblox Studio. Studio remains the workspace; RBXForge drives it.
- **Not** an arbitrary mutation tool. The AI does not directly manipulate Roblox arbitrarily —
  it uses a constrained set of RBXForge tools.
- **Not** a fully autonomous game builder yet. Full gameplay-feature autonomy is a long-term
  goal, not a current capability.
- **Not** tied to one AI provider. RBXForge is provider-agnostic.
- **Not** a validation substitute for the developer. The developer can review changes in Studio.

## Important Architectural Principles

1. **The AI model is not the core of RBXForge.** The core is the architecture:
   `AI model → agent → RBXForge tool system → Studio plugin → Roblox Studio`.
2. **Changes flow through the Studio plugin.** The plugin is the only bridge into Roblox Studio.
3. **The AI uses Roblox-specific tools, not arbitrary manipulation.** Tools like `create_part`,
   `modify_instance`, and `run_luau` are the interface between the agent and Studio.
4. **Project awareness matters.** RBXForge should understand the existing project and avoid
   blindly creating duplicate systems.
5. **Verify everything.** Every change should be verified; failures should be diagnosed and fixed.
6. **Provider-agnostic.** Ollama is the initial preferred backend; NVIDIA NIM is an optional
   backend; more providers should be added without rewriting the agent.
7. **Small milestones.** Development proceeds through small, verifiable milestones, never by
   combining major milestones into one implementation.
8. **Autonomous by default.** RBXForge executes automatically rather than asking for
   confirmation on every operation.

## Related Documents

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Intended system architecture and diagrams |
| [ROADMAP.md](./ROADMAP.md) | Staged development roadmap |
| [DECISIONS.md](./DECISIONS.md) | Recorded architecture decisions (ADR-style) |
| [AGENT.md](./AGENT.md) | How the AI agent is expected to behave |
| [TOOLS.md](./TOOLS.md) | Planned RBXForge tool system |
| [PLUGIN.md](./PLUGIN.md) | Planned Roblox Studio plugin |
| [AI.md](./AI.md) | AI provider architecture |
| [PROTOCOL.md](./PROTOCOL.md) | Communication protocol (draft) |
| [DEVELOPMENT.md](./DEVELOPMENT.md) | Development rules for future coding agents |
