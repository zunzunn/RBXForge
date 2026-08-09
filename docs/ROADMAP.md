# RBXForge — Roadmap

> **Status:** Planned. No dates are assigned. Phases are ordered and build on one another.
> Each phase must be verified before the next one begins.

## Roadmap Rules

- One focused capability per milestone.
- Verify before proceeding.
- Fix failures before moving forward.
- Do not combine multiple major milestones into one implementation.
- These phases are **not** claims about what exists today — they are a plan.

---

## Phase 0 — Project Definition

**Goal:** Establish an authoritative, readable project context that future agents use before
making changes.

**Deliverables:**

- `/docs` documentation set (this roadmap included).
- Root `README.md`.
- Recorded architecture decisions.

**Verification criteria:**

- All planned documentation files exist.
- Documents do not contradict each other.
- Architecture matches the recorded decisions.
- No implementation code exists yet.

**Dependencies:** None.

**Explicitly NOT included:** Any CLI, agent, plugin, protocol, or tool implementation.

---

## Phase 1 — Studio Connection

> **Status:** In progress. The minimal channel, plugin, and client exist
> (`cli/rbxforge.py`, `plugin/rbxforge.lua`, protocol in [PROTOCOL.md](./PROTOCOL.md)).
> The CLI↔plugin protocol is verified with automated tests; the plugin still needs a final
> verification run inside Roblox Studio (see the "How to run" notes in the repo README).

**Goal:** RBXForge can communicate with the Roblox Studio plugin.

**Deliverables:**

- A minimal communication channel (local WebSocket connection, per the protocol in
  [PROTOCOL.md](./PROTOCOL.md)). **Done** — `hello`/`welcome` handshake and `ping`/`pong`.
- A minimal Studio plugin that connects and can acknowledge requests. **Done** —
  `plugin/rbxforge.lua`.
- A minimal client side that can open the connection. **Done** — `cli/rbxforge.py` runs the
  local WebSocket server and logs connections/disconnections.

**Verification criteria:**

- RBXForge can send a message to the plugin and receive a response.
- The connection is local and requires no external server.
- The plugin can be loaded inside Roblox Studio without errors.

**Dependencies:** Phase 0.

**Explicitly NOT included:** Creating or modifying actual Studio objects yet. Full tool system.
AI agent.

---

## Phase 2 — Basic Studio Tools

**Goal:** Create and modify simple Roblox Instances.

**Deliverables:**

- A small, working set of tools (e.g. create / modify / move simple objects).
- The plugin implements the corresponding Studio operations.

**Verification criteria:**

- A tool can create an object that actually appears in the open Studio project.
- A tool can modify an existing object.
- Each tool returns a success or failure response.

**Dependencies:** Phase 1.

**Explicitly NOT included:** AI agent. Complex structures. Gameplay logic.

---

## Phase 3 — First Agent Loop

> **Status:** In progress. Phase 3A (provider layer) and Phase 3B (minimal single-step agent) are
> **Done**: a provider-agnostic inference interface (`cli/providers.py`) with Ollama + mock
> backends and env-based configuration, and a minimal agent (`cli/agent.py`) that turns one
> natural-language prompt into a structured tool call executed through the `ToolRegistry`
> (see [AI.md](./AI.md)). The multi-step agent loop itself is **not** implemented yet.

**Goal:** Prompt → tool selection → execution → verification.

**Deliverables:**

- An interactive agent that receives a prompt, selects tools, executes them, and reports.
- Basic verification of the outcome.

**Verification criteria:**

- `RBXForge > create a red cube` results in a red cube in Studio, verified and reported.
- The loop handles a simple failure by diagnosing and reporting it.

**Dependencies:** Phases 1 and 2.

**Explicitly NOT included:** Full project awareness. Complex multi-step feature construction.

---

## Phase 4 — Project Awareness

**Goal:** Inspect and understand the existing project.

**Deliverables:**

- Project inspection tooling (e.g. inspect the tree, search instances).
- Indexing that loads only relevant context into the AI.
- Behavior that avoids duplicate systems where possible.

**Verification criteria:**

- The agent can answer what exists in the project (e.g. find "Town").
- The agent plans changes around existing systems instead of duplicating them.

**Dependencies:** Phase 3.

**Explicitly NOT included:** Full autonomous gameplay construction.

---

## Phase 5 — Building Systems

**Goal:** Models, structures, and UI.

**Deliverables:**

- Multi-object modeling (models, groups, folders).
- UI creation.
- Reusable structures (e.g. a small shop).

**Verification criteria:**

- `RBXForge > build a small medieval shop` produces a coherent multi-object structure.
- `RBXForge > add UI` produces UI that appears in Studio.

**Dependencies:** Phases 3 and 4.

**Explicitly NOT included:** Gameplay logic and scripts beyond the simplest cases.

---

## Phase 6 — Gameplay Logic

**Goal:** Luau and complete gameplay features.

**Deliverables:**

- Script creation and modification.
- RemoteEvents / RemoteFunctions.
- Data systems, inventories, combat, quests, NPCs — progressively.

**Verification criteria:**

- Scripts are created and placed correctly.
- Simple gameplay features function (verified by the agent where possible).

**Dependencies:** Phases 3, 4, 5.

**Explicitly NOT included:** Full autonomous debugging and self-correction at scale.

---

## Phase 7 — Autonomous Game Development

**Goal:** Advanced planning, debugging, and multi-step feature construction.

**Deliverables:**

- More advanced planning across multiple features.
- Automated diagnosis and fixing of failures.
- Long multi-step feature construction sessions.

**Verification criteria:**

- RBXForge completes multi-step features with verification at each step.
- Failures are diagnosed and fixed without starting from scratch.

**Dependencies:** Phases 3–6.

**Explicitly NOT included:** Anything that cannot be verified inside the open Studio project.

---

## No Dates

This roadmap intentionally assigns **no dates**. Order and dependencies matter; calendar
estimates are avoided until the system is real and measurable.

## Status Legend

| Status | Meaning |
| --- | --- |
| Not started | Phase has not begun |
| In progress | Work is underway |
| Done | Delivered and verified |

| Phase | Status |
| --- | --- |
| Phase 0 — Project Definition | **Done** |
| Phase 1 — Studio Connection | **In progress** |
| Phase 2 — Basic Studio Tools | **In progress** |
| Phase 3 — First Agent Loop | **In progress** (3A provider layer + 3B single-step agent done) |
| Phase 4 — Project Awareness | Not started |
| Phase 5 — Building Systems | Not started |
| Phase 6 — Gameplay Logic | Not started |
| Phase 7 — Autonomous Game Development | Not started |
