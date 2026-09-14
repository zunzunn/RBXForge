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

> **Status:** In progress. Phase 5A (create_part color enum), Phase 5B
> (create_part physics flags: `anchored` / `can_collide`), and Phase 5C
> (create_part material enum with default `"Plastic"`) are **Done**.

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

### Phase 7A — Asset Discovery (Open Cloud Creator Store)

> **Status:** Implementing / done. `asset_search` (read-only Creator Store search over the
> Open Cloud API) is implemented and verified with automated tests.

**Goal:** let the agent discover **real** Roblox assets (models, decals, audio, plugins,
meshes, videos, font families) without touching the open Studio project.

**Deliverables:**

- `cli/roblox_assets.py`: a read-only Open Cloud Creator Store search client (API key from the
  environment, `x-api-key` header, HTTP 429 retry with backoff, typed errors, defensive parsing).
- `asset_search` tool registered in the CLI `ToolRegistry` and exposed to the REPL
  (`asset_search <query> [max_results]`), the one-shot CLI (`--asset-search-once`), and the
  agent (never an action tool — it does not end the loop).
- Bounded structured results: `{ query, asset_type, max_results, count, total, truncated,
  results }`.

**Verification criteria:**

- Automated tests cover config, request/response parsing, HTTP errors, rate limiting,
  timeouts, schema validation, REPL/one-shot exit codes, and agent exposure.
- The tool never sends a WebSocket `request` and never modifies the project/plugin.

**Dependencies:** Phases 3, 4.

**Explicitly NOT included:** inserting/cloning/downloading/purchasing assets; the general
autonomous-development goals of Phase 7.

### Phase 7B — Asset Ranking / Recommendation (Open Cloud Creator Store)

> **Status:** Done. `recommend_assets` (a bounded, deterministic ranking layer on top of the
> Phase 7A read-only search) is implemented and verified with automated tests.

**Goal:** let the agent turn a natural-language request (e.g. "recommend a good shop model")
into a short, **explainable** list of real Creator Store assets it can speak to with confidence.

**Deliverables:**

- `cli/asset_ranking.py`: a pure, deterministic, read-only ranking layer. Scores each returned
  result from its metadata (title/description term matches, exact-phrase bonus, asset-type
  synonym/hint, creator match, and a capped rating/usage bonus), sorts by score then name then
  id, bounds the output to `MAX_RECOMMENDATIONS` (5, default 3), and emits a human-readable
  `reason` per recommendation. Typed `RankingError` (subclass of the Phase 7A `AssetError`).
- The Phase 7A parser now also captures optional `rating`/`sales_count`/`favorite_count` usage
  metadata when the API provides it (backward-compatible — the Phase 7A shape is unchanged when
  it is absent).
- `recommend_assets` tool registered in the CLI `ToolRegistry` and exposed to the REPL
  (`recommend_assets <query> [limit]`), the one-shot CLI (`--recommend-assets-once`, plus
  `--creator` / `--max-recommendations`), and the agent (never an action tool — it does not end
  the loop).
- Bounded structured result: `{ query, asset_type, creator, evaluated, limit, count,
  recommendations[{rank, score, reason, asset}], tiebreak, note }`.

**Verification criteria:**

- Unit tests cover ranking signals, deterministic ties, missing metadata, invalid inputs, empty
  results, and result limits; integration/agent tests cover parser metadata, tool execution
  (params, failures, exactly-one-API-call), and the full `asset_search` → ranking →
  `recommend_assets` → report agent flow.
- Ranking makes **no additional API calls**, never sends a WebSocket `request`, and never
  modifies the project/plugin.

**Dependencies:** Phase 7A.

**Explicitly NOT included:** inserting/cloning/downloading/purchasing assets; any mutation; the
general autonomous-development goals of Phase 7.

### Phase 7C — Asset Selection and Studio Insertion

> **Status:** Done. `insert_asset` (a plugin tool that loads a Creator Store asset
> by the exact id a prior search/ranking returned and places it in the project)
> is implemented and verified with automated tests.

**Goal:** let the agent complete the full loop end-to-end — search real Creator Store
assets (7A), rank them (7B), **select** the appropriate one, and **insert** it into the
currently connected Studio project at a specified or sensible location.

**Deliverables:**

- **Known-id enforcement:** `asset_search` / `recommend_assets` record the asset ids they
  returned in a **bounded** per-session registry (`MAX_KNOWN_ASSETS` = 200) on the
  connection; `insert_asset` accepts **only** an id recorded there, so the model can never
  invent an id. The plugin's id is digits-only and validated before anything is sent.
- `insert_asset` tool registered in the CLI `ToolRegistry` (schema: `asset_id` required,
  digits-only bounded string; optional `parent_path`, `position` (vec3), `reference_path` —
  `position` and `reference_path` are mutually exclusive). Exposed to the REPL
  (`insert_asset <asset_id> [parent_path]`), the one-shot CLI (`--insert-asset-once` plus
  `--asset-id` / `--parent-path` / `--position` / `--reference-path`; the human-typed id is
  an explicit exception that seeds the registry), and the agent as an **action tool** (the
  loop ends after one optional `inspect_instance` verification step, mirroring
  `modify_instance`).
- Plugin handler (`plugin/rbxforge.lua`): re-validates, resolves the game-rooted
  `parent_path`, `InsertService:LoadAsset` inside pcall, ensures a **sibling-unique name**
  (bounded suffix scan), parents the asset, and positions it — explicit `position`, near a
  `reference_path` (+ `PLACEMENT_OFFSET`), or defaulted beside the project's first
  `SpawnLocation` (else a sensible spot above the origin). Models pivot (`PivotTo` with a
  `SetPrimaryPartCFrame` fallback), bare parts get an absolute `Position`; assets that carry
  no transform (e.g. decal/audio) are reported `positioned: false` instead of silently
  adjusted. Result carries `asset_id`, `name`, `class`, `parent_path`, `path`,
  `positioned`, `placement` (`explicit`/`reference`/`default`), and `position` when set.
- No new protocol version: `insert_asset` uses the same `request`/`response` messages as
  every other plugin tool. No purchase, download, or deletion features.

**Verification criteria:**

- Automated tests cover schema validation, invented/unknown-id rejection (never sent),
  non-insertable asset-type rejection, missing-search-result rejection, `position` vs
  `reference_path` exclusivity, the bounded registry, success/failure round-trips over the
  protocol, `--insert-asset-once` usage errors, duplicate-name unique-suffix passthrough,
  and the full agent flow `asset_search` → ranking → `insert_asset` → `inspect_instance`.
- Insertion only ever follows an id the model actually discovered (or, for the one-shot
  CLI, an id the human explicitly typed); every failure is a clear, loggable error.

**Dependencies:** Phases 7A and 7B (plus the 6B verification behavior).

**Explicitly NOT included:** purchasing assets, arbitrary external downloads, asset
deletion, or unrelated project changes.

### Phase 7D — Asset Insertion Verification and Reliability

> **Status:** Done. After `insert_asset` reports success, the Agent automatically
> verifies the placed instance with `inspect_instance` at the reported path and
> fails closed when verification does not match; the plugin also defends against
> silent parenting failures, duplicate-name exhaustion, and inconsistent paths.

**Goal:** make the end-to-end asset workflow reliable: search (7A) → rank (7B) →
select → insert (7C) → **inspect/verify**. The Agent must never claim success when
the expected instance is missing, the returned path is wrong, or the inspected
properties do not match the insertion report.

**Deliverables:**

- **Agent-side automatic verification:** after a successful `insert_asset`, the
  Agent immediately executes `inspect_instance` at the path the plugin returned and
  compares path, name, and class. Any mismatch (including a missing instance,
  wrong path, or name/class mismatch) is reported as `verification_failed`; the
  result is **not** presented as a successful insertion.
- **Graceful handling of delayed/missing responses:** `insert_asset` already
  respects the per-tool timeout; a missing plugin response is reported as a
  failure, not success.
- **Plugin-side reliability checks:** `plugin/rbxforge.lua` verifies that the
  loaded asset is actually parented in the project (`loaded.Parent == parent` and
  `IsDescendantOf(game)`), caps the sibling-unique name scan and reports an error
  if no unique name can be found, and verifies that the reported path round-trips
  back to the inserted instance before returning success.
- **Duplicate-name handling:** the plugin's `uniqueSiblingName` appends a bounded
  numeric suffix ("Cafe Shop2", "Cafe Shop3", ...) and errors out if the scan
  exhausts its budget; the Agent verifies at the *returned* path so it reports the
  actual unique name.
- **Bounded tool calls preserved:** automatic verification adds exactly one
  `inspect_instance` call after a successful `insert_asset`; no unbounded loop is
  introduced.
- **No new protocol version:** verification reuses the existing `insert_asset` and
  `inspect_instance` `request`/`response` messages.

**Verification criteria:**

- Automated tests cover successful verification, missing inserted instance,
  delayed/timeout response, duplicate-name suffix handling, incorrect returned path,
  name/class mismatch, insertion failure, and the full Agent flow
  `asset_search` → ranking → `insert_asset` → automatic `inspect_instance`.
- The Agent never reports success when verification fails; every failure path
  produces a clear `verification_failed` or `execution_failed` result.

**Dependencies:** Phases 7A, 7B, and 7C.

**Explicitly NOT included:** purchasing assets, arbitrary external downloads,
asset deletion, or unrelated project changes.

### Phase 8A — Scene-Aware Building

> **Status:** Done. The Agent can declare a multi-object `build` plan, inspect the
> existing scene, execute a bounded sequence of create/insert actions, and have
> every created path verified before the build is reported complete.

**Goal:** let RBXForge understand the existing Roblox scene and intelligently
combine primitives, scripts, and Creator Store assets into a coherent structure
for natural requests like "build a small shop near the SpawnLocation" or "make a
simple garage next to the existing house".

**Deliverables:**

- **``build`` orchestration tool:** a new registered tool that does not change the
  project but signals multi-object build mode. It accepts a `description` and an
  optional `reference_path` to an existing instance to place near. When the Agent
  executes `build`, the per-request tool-call budget is raised to a bounded
  `BUILD_MODE_MAX_TOOL_CALLS` (12) so several create/insert actions can run in one
  request.
- **Reuses existing tools:** `build` is pure orchestration; the model still calls
  `inspect_instance` / `find_instances` / `inspect_hierarchy` to read the scene,
  then `create_part`, `create_script`, `insert_asset`, and `modify_instance` to
  execute the plan. No new primitive/script/asset logic was duplicated.
- **Bounded and deterministic:** no uncontrolled recursive planning, no arbitrary
  code execution, no asset purchasing/deletion. The loop stays bounded by the
  raised budget and ends on the model's final report or budget exhaustion.
- **Tracked intermediate state:** during build mode the Agent records the path of
  every successful action tool. Step failures are recorded but the loop continues
  so the model can abort cleanly with a final report.
- **Final verification:** when the model sends a final report in build mode, the
  Agent automatically calls `inspect_instance` for every recorded path and
  confirms the instance exists. If any step failed or any path cannot be
  confirmed, the result is `build_failed` — the Agent never reports a completed
  build when part of the plan failed.

**Verification criteria:**

- Automated tests cover scene inspection before building, multi-object
  construction, relative positioning via `reference_path`, mixed primitive +
  Creator Store asset builds, partial step failure, final verification failure,
  raised tool-call budget in build mode, and natural-language final reporting.
- The Agent never reports success when a build step fails or final verification
  fails; every incomplete build produces a clear `build_failed` result.

**Dependencies:** Phases 7A, 7B, 7C, and 7D.

**Explicitly NOT included:** arbitrary external downloads, asset purchasing,
asset deletion, unbounded planning loops, or generalized code generation beyond
script creation through the existing `create_script` tool.

### Phase 8B — Intelligent Build Planning

> **Status:** Done. The model submits a structured, bounded construction plan
> with `plan_build`; the Agent validates it, tracks execution, skips redundant
> scene inspections, and always reports what was built in plain language.

**Goal:** turn natural-language requests like "build a small shop near the
SpawnLocation" into explicit, bounded construction plans before any project
changes happen, so the Agent can execute deterministically and explain the
result.

**Deliverables:**

- **`plan_build` orchestration tool:** a new registered tool used inside `build`
  mode. It accepts a `description`, optional `reference_path`, and a bounded
  `steps` array (max `PLAN_MAX_STEPS` = 5). Each step names a tool and its
  arguments. The tool validates the plan: steps must use allowed project
  read/write tools, must not contain duplicate consecutive steps, and must stay
  within the step limit. It does not execute the plan; execution still goes
  through the normal tool loop one step at a time.
- **Plan tracking in the Agent loop:** after `plan_build` succeeds, the Agent
  remembers the planned steps. As the model executes each tool, the Agent
  advances through the plan when the actual call matches the next expected step.
- **Redundant inspection avoidance:** the Agent caches successful
  `inspect_instance` results during build mode and reuses them when the model
  asks for the same path again, eliminating unnecessary plugin round-trips.
- **Natural-language summary:** when the model's final report is empty or
  missing, the Agent generates a simple fallback summary from the recorded
  created paths so the user always knows what was built.
- **Bounded and deterministic:** planning is capped at 5 steps, invalid plans
  are rejected, and the loop remains bounded by the raised build-mode budget.

**Verification criteria:**

- Automated tests cover successful structured plans, `plan_build` outside build
  mode, disallowed tools in a plan, duplicate consecutive steps, oversized
  plans, redundant `inspect_instance` skipping, and fallback summary generation.
- The Agent rejects invalid plans before executing them and never reports a
  completed build when the plan or any step fails.

**Dependencies:** Phase 8A.

**Explicitly NOT included:** arbitrary external downloads, asset purchasing,
asset deletion, unbounded planning loops, or generalized code generation beyond
script creation through the existing `create_script` tool.

### Phase 8D — Iterative Build Editing

> **Status:** Done. The Agent remembers a lightweight context of recently-built
> objects, reads it on `edit_build`, and applies the smallest set of changes
> (modify, create, or delete) to satisfy follow-up requests like "make the shop
> bigger" or "remove the sign you just created". Deletion is restricted to the
> recent build context, and every edit is verified before it is reported complete.

**Goal:** let RBXForge understand follow-up requests that modify an existing
build instead of rebuilding from scratch.

**Deliverables:**

- **Lightweight build context:** after a successful `build`, the Agent stores a
  bounded list of created objects (path, name, class, and captured properties).
  The context persists across requests so the next prompt can refer to "the shop"
  or "the sign you just created".
- **`recent_build_context` tool:** a read-only tool that returns the stored
  context. The model uses it at the start of an edit to identify the relevant
  objects without relying solely on name search.
- **`edit_build` orchestration tool:** signals iterative edit mode. Like `build`,
  it raises the per-request tool-call budget and keeps action tools as
  intermediate steps until a final report.
- **`delete_instance` tool:** removes an instance by path. The Agent layer only
  allows deletion of paths in the recent build context or touched in the current
  edit, preventing arbitrary project deletion.
- **Smallest-change guidance:** the system prompt instructs the model to prefer
  `modify_instance` over creating duplicates, and to use `delete_instance` only
  for explicit removal.
- **Edit verification:** when the model sends a final report in edit mode, the
  Agent verifies that modified/created paths exist and that deleted paths are
  gone. Partial failures report `build_failed`.

**Verification criteria:**

- Automated tests cover modifying a previous build, relative movement, resizing,
  property changes, adding components, explicit deletion, deletion safety,
  ambiguous references, avoiding unnecessary rebuilds, partial failure, and final
  verification.
- The Agent never deletes an object outside the recent build context or current
  edit, and never reports a completed edit when a step or verification fails.

**Dependencies:** Phases 8A and 8B.

**Explicitly NOT included:** arbitrary code execution, unrestricted deletion,
asset purchasing, unbounded planning loops, or a separate autonomous-agent
framework.

### Phase 9A — Robust Scene Understanding

> **Status:** Done. `analyze_scene` returns a bounded, deterministic summary of
> the Workspace — landmarks, major models, related-object groups, class counts,
> and optionally query-relevant instances — so the model can understand larger
> scenes before building or editing instead of repeatedly calling low-level
> inspection tools.

**Goal:** give RBXForge a lightweight, reusable scene-analysis capability that
helps the model plan and edit builds in larger, more complex Roblox projects.

**Deliverables:**

- **`analyze_scene` tool:** a read-only tool that internally calls
  `inspect_hierarchy` (and optionally `find_instances` when a query is given),
  then returns a compact summary. It is Agent-side orchestration: the internal
  inspection calls are not exposed to the model as separate tool results.
- **Summary contents:**
  - **Landmarks** — SpawnLocation, Baseplate, Camera, and any instance whose
    class is in a small landmark set.
  - **Major models/folders** — Models and Folders with their child counts and
    paths.
  - **Groups** — instances that share a name prefix (e.g. `Shop_Floor`,
    `Shop_Wall`, `Shop_Roof` are grouped as `Shop`).
  - **Class counts** — how many instances of each class appear in the scene.
  - **Relevant objects** — when the caller provides a `query`, the top
    `find_instances` matches are included.
- **Bounded and deterministic:** `analyze_scene` respects configurable `depth`
  (1..6, default 3) and `max_nodes` (1..500, default 200) limits, truncates
  output, and processes the tree deterministically. It gracefully handles empty
  scenes, missing metadata, and unusual hierarchies.
- **Integration:** the system prompt instructs the model to use `analyze_scene`
  at the start of complex builds or edits or when the user refers to the
  existing scene.

**Verification criteria:**

- Automated tests cover empty scenes, large scenes with truncation, nested
  models, grouped structures, relevant-object filtering, duplicate names across
  parents, missing metadata, bounded output, and Agent integration where
  `analyze_scene` is used before a `build`.
- The tool never changes the project, never traverses without bound, and
  returns a useful summary for scenes of varying size and shape.

**Dependencies:** Phases 4A, 4B, and 8D.

**Explicitly NOT included:** computer vision, arbitrary code execution,
unrestricted traversal, or a separate autonomous-agent framework.

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
| Phase 4 — Project Awareness | **In progress** (4A basic inspection done: `inspect_hierarchy`; 4B hierarchy search done: `find_instances`; 4C single-instance inspection done: `inspect_instance`; 4D AI project context / bounded multi-step agent loop done; 4E hosted Groq provider done; 6C verification behavior extensions) |
| Phase 5 — Building Systems | **In progress** (5A color enum done; 5B physics defaults done; 5C material enum/default + validation done) |
| Phase 6 — Gameplay Logic | **In progress** (6A `create_script` done: script creation with type/parent/source; 6B `modify_instance` done: allowlisted property changes on existing instances; 6C verification behavior extensions) |
| Phase 7 — Autonomous Game Development | **In progress** (7A asset discovery done: `asset_search` searches the public Roblox Creator Store over the Open Cloud API — read-only, local HTTP, no plugin/Studio changes; exposed to the REPL, the one-shot CLI, and the agent without ending the agent loop; 7B asset ranking done: `recommend_assets` ranks search results into a bounded, deterministic, explainable recommendation list — read-only, no extra API calls, likewise exposed; 7C asset insertion done: `insert_asset` inserts a Creator Store asset by an id a prior search/ranking returned — ids are never invented, placement is explicit/near-a-reference/default; 7D verification done: after `insert_asset` the Agent automatically verifies the placed instance with `inspect_instance` and fails closed on mismatch/missing/timeout, and the plugin defends against silent parenting failures, duplicate-name exhaustion, and inconsistent paths) |
| Phase 8 — Coherent Scene Construction | **In progress** (8A scene-aware building done: `build` orchestration tool enters multi-object build mode with a bounded raised tool-call budget; the Agent inspects the scene, executes multiple `create_part` / `create_script` / `insert_asset` / `modify_instance` steps, and verifies every created path before reporting success; partial failures and verification failures report `build_failed`. 8B intelligent build planning done: `plan_build` validates and stores a structured bounded plan inside build mode, the Agent tracks plan execution, skips redundant `inspect_instance` calls, and generates a natural-language summary of what was built. 8D iterative build editing done: lightweight `recent_build_context` persists across requests, `edit_build` activates edit mode, `delete_instance` removes objects from the recent build context, and the Agent verifies modified/created/deleted paths before reporting success. 9A robust scene understanding done: `analyze_scene` returns a bounded, deterministic summary of landmarks, models, groups, class counts, and query-relevant objects so the model can understand larger scenes before acting) |
