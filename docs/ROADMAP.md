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

> **Status:** In progress. Phase 3A (provider layer), Phase 3B (single-step agent), and
> Phase 3C (interactive AI REPL) are **Done**: a provider-agnostic inference interface
> (`cli/providers.py`) with Ollama + mock backends and env-based configuration; an agent
> (`cli/agent.py`) that turns one natural-language prompt into a structured tool call executed
> through the `ToolRegistry`; and a REPL where ordinary text reaches Studio via the agent
> (see [AI.md](./AI.md)). The multi-step agent loop was delivered in **Phase 4D** (below) as a
> bounded inspect → act loop, and a hosted **Groq** backend was added in **Phase 4E** (below);
> a full plan → verify → fix cycle is still future work.

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

**Phase 4A (done):** basic project inspection — the `inspect_hierarchy` tool snapshots the
current `workspace` as a bounded Name/ClassName tree (configurable depth, default 3, max 50,
with truncation flagged). Implemented end-to-end (CLI registry + validation, plugin handler,
protocol, docs, tests). Deliberately minimal: **no** search, indexing, spatial reasoning,
caching, or arbitrary property serialization.

**Phase 4B (done):** hierarchy search — the `find_instances` tool searches the live Workspace
hierarchy by instance name (case-insensitive substring match) and returns a bounded list of
`{ name, className, path }` matches (default 20, max 100) with a total count and truncation
flag. Implemented end-to-end (same CLI registry + validation, plugin handler, protocol, docs,
tests). No protocol changes were needed. Deliberately minimal: it reads the live hierarchy on
every request — **no** caching or indexing yet — and does not do arbitrary property inspection.

**Phase 4C (done):** single-instance inspection — the `inspect_instance` tool resolves one
instance by its full path and returns its identity (`name`, `className`), full path, parent
path, and a small **allowlisted** set of safe properties (BasePart / SpawnLocation / Model /
GuiObject; see [TOOLS.md](./TOOLS.md)). Implemented end-to-end (same CLI registry + validation,
plugin handler, protocol, docs, tests). No protocol changes were needed beyond one new `not_found`
error code. Deliberately minimal: strict path validation, **no** arbitrary property reflection,
**no** recursive descendant inspection, and **no** caching/indexing — each path is resolved
against the live hierarchy per request.

**Phase 4D (done):** AI project context — the **bounded multi-step agent loop**. The agent can
inspect the live Roblox project when needed *before* executing a tool:

- The model may call `find_instances` or `inspect_instance` (and `inspect_hierarchy`) to gather
  context; each successful tool result is returned to the model as a **bounded, compacted**
  payload (capped lists, truncated strings, a hard serialized-character budget — unbounded
  hierarchy/property data is never exposed to the model).
- The loop eventually executes an action tool such as `create_part`, then returns a concise final
  `AgentResult`.
- **Safety:** at most **5 tool calls per request** (`max_tool_calls`); every call goes through the
  existing `ToolRegistry` with validation unchanged; no arbitrary Lua/code execution; no new
  Studio tools; no automatic modification except through the existing action tools; the loop
  returns a clear failure (`unknown_tool` / `invalid_arguments` / `execution_failed` /
  `provider_error` / `malformed_output` / `max_tool_calls`) instead of guessing; single-step
  requests behave exactly as before.
- Implemented in `cli/agent.py` only — **no** new tools, **no** plugin/WebSocket protocol
  changes. Deterministic multi-step tests use the mock provider (no model needed).

**Phase 4E (done):** a second real AI backend — **Groq**. `GroqProvider` in `cli/providers.py`
speaks Groq's OpenAI-compatible chat API (`POST {base_url}/chat/completions`), selected with
`RBXFORGE_PROVIDER=groq`:

- Configured via `RBXFORGE_API_KEY` (required; never hard-coded — missing key raises
  `ProviderConfigError` up front), `RBXFORGE_MODEL`, and `RBXFORGE_BASE_URL` (default
  `https://api.groq.com/openai/v1`; kept distinct from Ollama's default).
- Same `Provider` interface, same typed errors, and the same JSON-in-text tool calling as
  Ollama — the Phase 3B/4D agent and the interactive REPL work unchanged against a hosted model.
- No API calls to real Groq in tests; a fake in-process `/chat/completions` server covers
  selection, configuration, success (request body + Bearer auth verified), timeout, connection
  error, HTTP/error payloads, and response parsing.
- **No** provider-native tool calling yet (still JSON-in-text); `OllamaProvider` and
  `MockProvider` are unchanged; **NVIDIA NIM is still not implemented**; no changes to Studio
  tools, the plugin, or the WebSocket protocol.

**Remaining deliverables:**

- Project inspection tooling (e.g. generalized search by class type / property).
- Indexing that loads only relevant context into the AI.
- Behavior that avoids duplicate systems where possible.
- Full plan → verify → fix cycling (beyond the bounded phase 4D loop).

**Verification criteria:**

- The agent can answer what exists in the project (e.g. find "Town") — now feasible via the
  Phase 4D loop's inspection tools.
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
| Phase 3 — First Agent Loop | **In progress** (3A provider layer + 3B single-step agent + 3C AI REPL done; bounded multi-step loop delivered in 4D, hosted Groq backend delivered in 4E) |
| Phase 4 — Project Awareness | **In progress** (4A basic inspection done: `inspect_hierarchy`; 4B hierarchy search done: `find_instances`; 4C single-instance inspection done: `inspect_instance`; 4D AI project context / bounded multi-step agent loop done; 4E hosted Groq provider done) |
| Phase 5 — Building Systems | Not started |
| Phase 6 — Gameplay Logic | Not started |
| Phase 7 — Autonomous Game Development | Not started |
