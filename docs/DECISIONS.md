# RBXForge — Decisions (ADR-style)

> **Status:** Each decision below carries its own status: `Accepted` (authoritative) or
> `Provisional` (accepted for now, but likely to change before or during implementation).
>
> These decisions are authoritative unless explicitly changed later. When a decision changes,
> update this file (see [DEVELOPMENT.md](./DEVELOPMENT.md)).

---

## D-001 — Product Name: RBXForge

- **Decision:** The product is named RBXForge.
- **Status:** Accepted.
- **Reason:** Distinguishes the product as a Roblox build tool (forge = build/create).
- **Alternatives considered:** RobloxForge, RBXBuild, RBXAgent.
- **Consequences:** All documentation, command names, and future code must use "RBXForge".

---

## D-002 — Primary Command: `rbxforge`

- **Decision:** The primary CLI command is `rbxforge`.
- **Status:** Accepted.
- **Reason:** Matches the product name and is short to type.
- **Alternatives considered:** `rbx`, `forge`, `rbxforge-build`.
- **Consequences:** The CLI entry point will expose this command. Packaging details are not yet
  defined.

---

## D-003 — Primary Interaction: Interactive AI Agent

- **Decision:** The primary interaction is an interactive AI session launched by `rbxforge`.
- **Status:** Accepted.
- **Reason:** Matches the product vision and keeps the UX focused on prompts like
  `RBXForge > add a shop`.
- **Alternatives considered:** One-shot commands (`rbxforge build shop`), a GUI, a daemon.
- **Consequences:** The agent loop is the centerpiece; other interaction modes may be added later
  but are not a priority.

---

## D-004 — Autonomy: Execute Without Per-Operation Confirmation

- **Decision:** RBXForge executes changes automatically and does not ask for confirmation for
  every normal operation.
- **Status:** Accepted.
- **Reason:** The intended agent behavior is Understand → Inspect → Plan → Execute → Verify →
  Fix if necessary → Verify again → Report.
- **Alternatives considered:** Confirm every operation; confirm only destructive operations.
- **Consequences:** Safety must be handled inside the tool system (e.g. careful delete
  semantics, undo/rollback considerations) rather than via blocking prompts. This is a real
  design challenge and should be revisited during implementation.

---

## D-005 — Roblox Studio Integration via a Dedicated Plugin

- **Decision:** RBXForge communicates with Roblox Studio through a dedicated Roblox Studio
  plugin. The plugin is the only bridge into Studio.
- **Status:** Accepted.
- **Reason:** Studio changes must go through Studio APIs available to plugins; a dedicated
  bridge keeps the AI from touching Studio directly.
- **Alternatives considered:** File-based editing of `.rbxl`/`.rbxlx`, Roblox Open Cloud APIs,
  direct in-process scripting.
- **Consequences:** The plugin is a critical, non-optional component. Its capability and
  reliability bound what RBXForge can do.

---

## D-006 — Local Communication: WebSocket-Style Connection

- **Decision:** The initial preferred communication approach between RBXForge and the plugin is
  a local WebSocket-style connection.
- **Status:** Provisional.
- **Reason:** A local WebSocket is a common, simple, bidirectional channel with broad support in
  both Lua/Roblox plugin contexts and general-purpose CLIs.
- **Alternatives considered:** HTTP polling, TCP sockets, shared files, standard input/output.
- **Consequences:** The protocol is drafted in [PROTOCOL.md](./PROTOCOL.md) and is marked Draft.
  The transport is **not yet implemented** and may change if Roblox plugin constraints make
  WebSockets impractical. **Provisional** until a working proof-of-concept exists.

---

## D-007 — AI Provider-Agnostic Architecture

- **Decision:** RBXForge is provider-agnostic; the agent does not depend on any single provider.
- **Status:** Accepted.
- **Reason:** Avoids lock-in and lets users choose local or hosted models.
- **Alternatives considered:** Hard-coding a single provider.
- **Consequences:** A provider abstraction is required. Providers plug in behind a stable
  interface (see [AI.md](./AI.md)).

---

## D-008 — Initial AI Backend: Ollama / Local Models

- **Decision:** The initial preferred AI backend is Ollama (local models).
- **Status:** Accepted.
- **Reason:** Local execution, privacy, no per-token cost, and works offline.
- **Alternatives considered:** Hosted APIs, NVIDIA NIM.
- **Consequences:** RBXForge should work well with Ollama first. Model capabilities will limit
  agent quality; tool-calling support must be considered when selecting models.

---

## D-009 — Optional AI Backend: NVIDIA NIM

- **Decision:** NVIDIA NIM should be supported as another possible backend.
- **Status:** Accepted.
- **Reason:** User requirement; provides a higher-capability hosted/accelerated option.
- **Alternatives considered:** None (specified by user).
- **Consequences:** The provider abstraction must accommodate NIM alongside Ollama.

---

## D-010 — AI Model Is Not the Core

- **Decision:** The AI model itself is not the core of RBXForge. The core is the architecture:
  `AI model → agent → RBXForge tool system → Studio plugin → Roblox Studio`.
- **Status:** Accepted.
- **Reason:** Keeps the system robust to model changes and focuses value on the RBXForge
  layers.
- **Alternatives considered:** A thin wrapper around a single strong model.
- **Consequences:** Investment should go into the tool system, plugin, protocol, verification,
  and project awareness, not into any single model.

---

## D-011 — Tool-Based Architecture (No Arbitrary Mutation)

- **Decision:** The AI does not directly manipulate Roblox arbitrarily. RBXForge provides
  Roblox-specific tools (e.g. `inspect_project`, `create_part`, `modify_instance`,
  `run_luau`, `verify`) that the agent calls.
- **Status:** Accepted.
- **Reason:** Constrains the AI to safe, understood operations and enables verification and
  rollback.
- **Alternatives considered:** Giving the model arbitrary Studio access.
- **Consequences:** The tool set is a key design surface; it is planned conceptually in
  [TOOLS.md](./TOOLS.md). Final APIs are not invented yet.

---

## D-012 — Project Awareness and Intelligent Indexing

- **Decision:** RBXForge should understand the existing project and avoid blindly creating
  duplicate systems, using intelligent project inspection/indexing, loading only relevant
  context when possible.
- **Status:** Accepted (as a goal).
- **Reason:** Prevents duplicate systems and incoherent changes.
- **Alternatives considered:** Acting on a blank-slate assumption.
- **Consequences:** Adds a project inspection/index component and context-loading design (see
  [ARCHITECTURE.md](./ARCHITECTURE.md)). This is a long-term, phased capability
  (Phase 4 in [ROADMAP.md](./ROADMAP.md)).

---

## D-013 — Full Agent Loop with Verify / Fix / Re-Verify

- **Decision:** RBXForge operates as a full agent loop: PROMPT → UNDERSTAND → INSPECT → PLAN →
  EXECUTE → VERIFY → (SUCCESS → DONE, or DIAGNOSE → FIX → VERIFY).
- **Status:** Accepted (as a goal).
- **Reason:** The goal is making the change actually work in Roblox Studio, not merely
  generating code.
- **Alternatives considered:** Single-shot generation without verification.
- **Consequences:** Verification becomes a first-class concept and a future component.

---

## D-014 — First Milestone: "create a red cube"

- **Decision:** The first actual functionality to build is extremely small: the agent creates a
  red cube that actually appears in the open Roblox Studio project. Progressive tests follow
  (modify → move → multiple objects → simple structure → small shop → UI → gameplay logic).
- **Status:** Accepted.
- **Reason:** Smallest end-to-end slice that proves the whole pipeline works.
- **Alternatives considered:** Starting with a larger feature.
- **Consequences:** This defines the shape of Phase 2/3 milestones. It is **not implemented
  yet**.

---

## D-015 — Small Milestone Development Strategy

- **Decision:** Development proceeds through small milestones; each milestone implements one
  focused capability, is run/verified, failures are inspected and fixed, then verification is
  re-run before moving on. Major milestones are never combined.
- **Status:** Accepted.
- **Reason:** Keeps progress verifiable and debuggable.
- **Alternatives considered:** Big-bang implementation.
- **Consequences:** See [DEVELOPMENT.md](./DEVELOPMENT.md) and [ROADMAP.md](./ROADMAP.md).

---

## D-016 — First Version Focus: Studio Objects, Not Gameplay

- **Decision:** The first version focuses on building/modifying Studio objects. Gameplay logic
  (scripts, Remotes, data systems) comes later.
- **Status:** Accepted.
- **Reason:** Matches the phased roadmap and reduces initial scope.
- **Alternatives considered:** Attempting full gameplay features immediately.
- **Consequences:** Phases 2–5 target objects; Phase 6 targets gameplay logic.

---

## D-017 — No Unnecessary Dependencies

- **Decision:** Do not introduce unnecessary dependencies; prefer simple implementations.
- **Status:** Accepted.
- **Reason:** Keeps the system maintainable and debuggable.
- **Alternatives considered:** Adopting heavyweight frameworks early.
- **Consequences:** Enforced as a development rule in [DEVELOPMENT.md](./DEVELOPMENT.md).

---

## D-018 — Documentation Before Implementation

- **Decision:** The project begins with authoritative documentation, and future agents must read
  the relevant `/docs` files before modifying the project.
- **Status:** Accepted.
- **Reason:** Provides a stable project context for future agents.
- **Alternatives considered:** Starting implementation immediately.
- **Consequences:** The `/docs` directory is the project source of truth for the current stage.

---

## Open / Unresolved Decisions

These are not yet decided and must be resolved before (or during) implementation:

- **U-001 — Tool safety semantics:** How delete operations, undo/rollback, and renames are made
  safe given the no-confirmation autonomy decision (D-004).
- **U-002 — Plugin transport feasibility:** Whether Roblox plugin constraints allow a WebSocket
  connection (D-006), or whether a fallback transport is needed.
- **U-003 — Serialization format:** The exact wire format of requests/responses
  (see [PROTOCOL.md](./PROTOCOL.md)).
- **U-004 — Project inspection scope:** How deep project indexing goes in Phase 4 (full tree
  vs. lazy relevant-subtree loading).
- **U-005 — Verification fidelity:** What "verified" means for each tool (existence? property
  equality? script execution?).
- **U-006 — Model selection:** Which concrete models/tool-calling capabilities are targeted for
  Ollama vs NIM (see [AI.md](./AI.md)).
- **U-007 — Project file format handling:** Whether RBXForge reads the `.rbxl`/`.rbxlx` file, or
  exclusively relies on the plugin's view of the live project.
- **U-008 — Undo integration with Studio:** How RBXForge changes integrate with Studio's own
  undo system.
