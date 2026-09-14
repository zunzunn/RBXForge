# RBXForge — AI / Provider Architecture

> **Status:** Provider layer + single-step agent + interactive AI REPL implemented
> (Phases 3A–3C); the **bounded multi-step agent loop with project inspection** is
> implemented in Phase 4D; a **second real provider (Groq, hosted)** is implemented in Phase 4E.
>
> **Implemented (Phase 6C):** the bounded multi-step agent loop with project inspection
> and optional verification. The model may call the inspection tools
> (`find_instances`, `inspect_instance`, `inspect_hierarchy`) to gather live project context;
> each tool result is returned to the model as a bounded, compacted payload, so it can decide
> the next step. It eventually executes an action tool (`create_part`, `create_script`,
> `modify_instance`, `insert_asset`). The loop is capped at **5 tool calls per request**, only executes through
> the existing `ToolRegistry`, and never exposes unbounded hierarchy/property data to the model.
>
> **Implemented (Phase 8A):** the `build` tool enables **scene-aware multi-object construction**.
> The model declares a build plan, inspects the scene, executes a bounded sequence of action
> tools, and the Agent verifies every created path before reporting success. Build mode raises
> the per-request budget to a bounded **12 tool calls**; partial failures and verification
> failures report `build_failed`, never a completed build.
>
> **Implemented (Phase 8B):** the `plan_build` tool makes planning explicit. Inside build mode the
> model submits a structured plan of up to 5 steps before executing them; the Agent validates the
> plan, tracks execution, skips redundant `inspect_instance` calls, and always reports what was
> built in plain language (with a generated fallback summary if the model's final report is empty).
>
> **Implemented (Phase 8D):** the Agent supports **iterative build editing**. It persists a
> lightweight `recent_build_context` across requests, exposes it via the `recent_build_context`
> tool, and uses `edit_build` to apply the smallest set of changes to an existing build. The model
> can modify, add, or (with `delete_instance`) remove objects from the recent build context; the
> Agent restricts deletion to recent build objects and verifies every modified/added/removed path
> before reporting success.
>
> **Implemented (Phase 9A):** the Agent has a **bounded, deterministic scene-summary** tool,
> `analyze_scene`. It reuses the existing inspection tools internally but only exposes a compact
> summary to the model — landmarks, major models/folders, grouped instances, class counts, and
> optional query-relevant objects. It is read-only, strictly bounded by `depth` and `max_nodes`,
> and the system prompt recommends it at the start of complex builds, edits, or whenever the user
> refers to the existing scene.
>
> **Implemented (Phase 7A):** `asset_search` is exposed to the model as a read-only Creator
> Store search tool (local HTTP, not a plugin tool). It is **not** an action tool: a successful
> search never ends the loop, and its dict result is compacted for the model the same way plugin
> results are.
>
> **Implemented (Phase 7B):** `recommend_assets` ranks those search results into a bounded,
> deterministic, explainable recommendation list (each entry has a score and a reason). It is
> likewise read-only and not an action tool: a successful recommendation never ends the loop,
> and the paired flow `asset_search` → `recommend_assets` → report lets the model state *why* an
> asset was chosen.
>
> **Implemented (Phase 7C):** `insert_asset` closes the loop — it inserts the Creator Store
> asset whose `asset_id` the model selected from a prior search/ranking result (ids are never
> invented; the connection records them in a bounded known-id registry). It **is** an action
> tool: after it succeeds the loop pauses for exactly one optional `inspect_instance` verification
> step (mirroring `modify_instance`), then ends with a report.

## Purpose

RBXForge is **provider-agnostic** (decision D-007). This document defines how the AI/provider
layer works. The model is interchangeable; the rest of the system is not built around any single
model.

```
User prompt
   ↓
Agent  ──►  AI Provider Layer  ──►  Model (Ollama / Groq / NIM / future)
                ▲
                │ model output (tool calls, text)
   Agent uses RBXForge tools
```

## Provider Abstraction (Implemented — Phase 3A)

- The agent depends on a **stable provider interface**, not on a specific provider (decision
  D-007). In Phase 3A that interface is the `Provider` base class in `cli/providers.py`.
- A provider is configured with a **model name**, an optional **base URL**, an optional **API
  key** (never hard-coded), and a request **timeout**.
- Every provider implements `chat(messages, **options) -> ProviderResponse`, where messages are
  `{"role", "content"}` dicts (`providers.message(role, content)`) and the response carries
  normalized `text` / `model` / `provider` fields.
- `build_provider(settings)` selects a provider by name (see Configuration below), and unknown
  names raise `ProviderConfigError`.
- `ProviderResponseError` / `ProviderConnectionError` / `ProviderTimeoutError` / ... normalize
  failures so callers can retry, degrade, or report regardless of which provider is active.
- Every HTTP request (Ollama and Groq alike) carries an explicit `RBXForge/0.1.0` User-Agent —
  hosted providers (Groq's Cloudflare layer) reject Python's default `urllib` User-Agent with
  `403 code 1010`, so the shared request layer sets one and still merges provided headers
  (`Content-Type`, `Authorization`, ...) untouched.

### Initial Preferred Backend: Ollama (local models) — implemented

- `OllamaProvider` speaks Ollama's local HTTP API (`POST {base_url}/api/chat`); the default base
  URL is `http://127.0.0.1:11434` (no API key needed).
- Runs models locally. Advantages: privacy, offline use, no per-token cost.
- Model name is configurable via `RBXFORGE_MODEL` (never hard-coded).

### Optional Backend: Groq (hosted models) — implemented (Phase 4E)

- `GroqProvider` speaks Groq's OpenAI-compatible chat API (`POST {base_url}/chat/completions`);
  the default base URL is `https://api.groq.com/openai/v1`.
- Requires an **API key** for the `Authorization: Bearer ...` header — read from
  `RBXFORGE_API_KEY`, never hard-coded. A missing key raises `ProviderConfigError` up front.
- Model name is configurable via `RBXFORGE_MODEL` (e.g. `llama-3.3-70b-versatile`); the request
  body keeps the OpenAI-compatible `{model, messages, stream, ...}` shape, and the reply is read
  from `choices[0].message.content`, so the agent's JSON-in-text tool calling works unchanged.
- Same typed errors as every provider (`ProviderTimeoutError`, `ProviderConnectionError`,
  `ProviderResponseError`, ...) — no raw `urllib` errors leak to callers.
- **GPT-OSS compatibility (Groq):** Groq defaults `tool_choice` to `none` when no `tools` are
  sent, so a tool-capable model like GPT-OSS that calls a tool natively is rejected with HTTP
  400 "Tool choice is none, but model called a tool". When the agent uses Groq it therefore
  sends the registry's tool definitions as Groq `tools` (with `tool_choice: "auto"`) and the
  provider normalizes any native `message.tool_calls` back into the JSON-in-text the agent
  parses (`{"tool", "arguments"}`) — the JSON-in-text agent architecture is unchanged, and
  Ollama/mock never receive `tools`.

### Optional Backend: NVIDIA NIM — recognized, not implemented

- `NimProvider` is a registered placeholder that raises `ProviderNotImplementedError`; it keeps
  the design NIM-compatible (decision D-009) but does no work yet.

### Future Providers

- The abstraction should allow additional providers (hosted APIs, other local runners, etc.)
  without rewriting the agent.

### Mock Provider (tests / experiments)

- `MockProvider` returns a configurable response from `chat` and can simulate timeouts and errors,
  so the agent (when built) can be tested without a live model.

## Model Configuration (Implemented — Phase 3A)

- No specific model is hard-coded; a model name is required (missing models raise
  `ProviderConfigError`).
- Configuration is read from environment variables by `ProviderSettings.from_env()`, and can
  also be passed programmatically to `ProviderSettings`.

| Variable            | Meaning                                                      | Default              |
| ------------------- | ------------------------------------------------------------ | -------------------- |
| `RBXFORGE_PROVIDER` | Provider name (`ollama`, `groq`, `nim`, `mock`)              | `ollama`             |
| `RBXFORGE_MODEL`    | Model name/identifier for that provider                      | *(required)*         |
| `RBXFORGE_BASE_URL` | Endpoint / base URL (e.g. Ollama server or Groq API)         | Provider default URL (Ollama local, or `https://api.groq.com/openai/v1` for Groq) |
| `RBXFORGE_API_KEY`  | API key (auth; never hard-coded in code)                     | *(empty)*            |
| `RBXFORGE_TIMEOUT`  | Request timeout in seconds                                   | `30`                 |
| `RBXFORGE_MOCK_RESPONSE` | Mock-only: what the mock provider returns verbatim (e.g. `{"tool": "create_part", ...}`) | `""`    |
| `RBXFORGE_MOCK_FAIL` | Mock-only: force a provider failure (`timeout`/`connection`/`response`) | *(empty)* |

Generation parameters (temperature, max tokens, ...) are passed through provider-specific
`chat()` options (e.g. Ollama's `"options"`).

## Interactive REPL (Implemented — Phase 3C)

`RBXForge> <text>` is now a real prompt-to-Studio pipeline:

```
RBXForge> create a red cube
   ↓
Agent.run(prompt)   ← plain REPL text is an AI prompt
   ↓
provider.chat(tool definitions + prompt)     (env-configured provider)
   ↓
parse + validate structured tool call        (unknown-tool / invalid-args rejected safely)
   ↓
ToolRegistry.execute  →  request over the existing WebSocket protocol
   ↓
Studio plugin           →   concise "[rbxforge] AI OK: called 'create_part' -> True"
```

- **Plain text = AI prompt.** Anything that is not a recognized command (`ping`, `create_part`,
  `status`, `help`, `quit`) is sent to `Agent.run` via `RBXForge.ask()`.
- **`ask <prompt>`** runs the agent explicitly (same path); a bare `ask` prints a gentle hint.
- **Results are concise**: one `[rbxforge] AI OK: called <tool> -> <output>` line on success, or
  one `[rbxforge] AI failed: <code>: <message>` line on any failure
  (`provider_error` / `malformed_output` / `unknown_tool` / `invalid_arguments` /
  `execution_failed`).
- **The REPL never crashes.** Provider errors, malformed output, unknown tools, and invalid
  arguments are all condensed into a log line; the prompt survives.
- **Prompt stability**: `ask` logs through the same `REPLConsole` as WebSocket events, so the
  prompt is re-drawn after any background log (Phase 2A behavior preserved).
- **Agent lifecycle**: the agent+provider are built lazily on the first prompt from the
  environment (`agent_from_env`, registry + connection wired to the running RBXForge) and
  reused afterwards.

## Agent / Tool Calling (Implemented — Phase 3B + Phase 4D + Phase 6C)

> **Implemented (Phase 3B):** `cli/agent.py` connects the provider layer to the Phase 2B tool
> layer as a **single-step** pass: `prompt → provider → tool call → execution` then return.
> **Extended (Phase 4D):** the same `Agent.run(prompt)` now drives a **bounded multi-step loop**
> with project inspection. Single-step behavior is preserved for simple requests.
> **Extended (Phase 6C):** the loop permits optional verification via `inspect_instance`
> after action tool success, conditional on whether the model previously called an inspection
> tool to gather project context.

The Phase 3B single-step flow (still how each individual tool call is handled):

1. **Give the AI the currently registered tool definitions.** The agent serializes
   `registry.list()` into the system prompt — each tool's `name`, `description`, and
   `parameters` (its `input_schema`) — and appends the user prompt.
2. **Provider.** `provider.chat(messages)` is called (Ollama in real use, mock in tests). Any
   `ProviderError` (timeout/connection/response/…) becomes a safe `provider_error` result.
3. **Parse.** The reply is parsed into either a structured tool call
   (`parse_agent_reply`: `{"tool": ..., "arguments": {...}}`) or a final report
   (`{"message": "..."}`). Malformed output becomes a safe `malformed_output` result.
4. **Validate + execute through the `ToolRegistry` only.** `registry.execute(...)` is the sole
   execution path — the same call used by the CLI tool layer. `UnknownToolError` → `unknown_tool`
   result; `InvalidParamsError` → `invalid_arguments` result. Both are rejected before anything
   is sent to the plugin. A falsy tool result becomes `execution_failed`.
5. **Result.** `AgentResult` carries `ok`, the parsed `tool`, the tool layer's `output`, an
   `error` dict (`code`/`message`/`type`), the raw `provider_text` for diagnostics, plus (Phase
   4D/6C) an optional final `message` and an ordered `steps` list of each call's outcome.

The Phase 4D/6C multi-step loop:

```
User prompt
   ↓
Agent loop (bounded, max 5 executed tool calls per request)
   ↓
model → {"tool": ..., "arguments": ...}
   ↓
ToolRegistry.execute  (validation unchanged)   → Studio (plugin) → tool result
   ↓
bounded compacted result is appended to the conversation
   ↓
model can call find_instances / inspect_instance / inspect_hierarchy / analyze_scene again, or act
    ↓
action tool succeeds → loop ends, unless the model previously called `build`
  (Phase 8A) or `edit_build` (Phase 8D). In build/edit mode action tools are
  intermediate steps; the loop ends when the model sends a final report, at which
  point the Agent verifies every created/edited/deleted path. The model may call
  `plan_build` (Phase 8B) after `build` to submit a structured bounded plan before
  executing it; the Agent validates the plan, tracks progress, and skips redundant
  `inspect_instance` calls. In edit mode the model should call
  `recent_build_context` to identify existing objects and prefer `modify_instance`
  over duplicate creation; `delete_instance` is allowed only for recent build
  objects.
  (create_part/create_script/modify_instance permit one optional
  model-driven inspect_instance verification step before ending when not in build
  mode; insert_asset automatically verifies with inspect_instance at the reported
  path and fails closed if the instance is missing or does not match).
```

`asset_search` (Phase 7A) and `recommend_assets` (Phase 7B) participate in the same loop but
execute as **local HTTP calls** (no plugin `request`), so `ToolRegistry.execute → Studio
(plugin)` does not apply to them. `asset_search` returns the bounded candidate list;
`recommend_assets` ranks that metadata in-process into a bounded, explainable recommendation
list (no extra API calls). Their structured dict results take the place of the plugin `response`
payload when the loop compacts what to show the model. `insert_asset` (Phase 7C/7D) is the
counterpart that does cross to the plugin: it only accepts an `asset_id` that a prior
`asset_search` / `recommend_assets` result recorded in the session's bounded known-id registry,
so the model bases every insertion on real discovered assets. Phase 7D makes the verification
automatic: after `insert_asset` succeeds, the Agent calls `inspect_instance` at the reported
path and reports `verification_failed` if the instance is missing or its name/class/path does
not match the insertion report. Phase 8A adds the `build` orchestration tool: after `build`,
action tools continue until a final report, and the Agent verifies every recorded created path
before reporting `build_failed` or success. Phase 8B adds the `plan_build` orchestration tool:
after `build`, the model can submit a structured plan of up to 5 steps; the Agent validates it,
tracks execution, caches `inspect_instance` results to skip redundant reads, and generates a
natural-language summary if the final report is empty. Phase 8D adds `edit_build`,
`recent_build_context`, and `delete_instance`: after `edit_build`, the model reads the recent
build context, applies the smallest set of changes to existing objects, and the Agent verifies
modified/created paths still exist and deleted paths are gone before reporting `build_failed`
or success. Phase 9A adds `analyze_scene`: it internally uses `inspect_hierarchy` / `find_instances` to produce a compact bounded summary, but those internal calls are not exposed to the model, and the system prompt recommends using it at the start of complex builds, edits, or any request that references the existing scene.

- The model replies with **one JSON object per step**: a tool call or a final report. The loop
  continues only while the model keeps choosing inspection tools successfully and the per-request
  budget remains.
- **Stopping:** the loop ends on an executed **action tool** (`create_part`, `create_script`,
  `modify_instance`, `insert_asset`, `delete_instance`) **unless the model previously called
  `build` or `edit_build`** (Phases 8A/8D), a final model report (`{"message": ...}`), a hard
  rejection (`unknown_tool` / `invalid_arguments` / `malformed_output` / `provider_error` /
  `execution_failed` / `verification_failed` / `build_failed` / `invalid_deletion`), or the
  **5-call budget** being exhausted (`max_tool_calls`; raised to a bounded 12 in build/edit mode).
  It stops rather than guessing when it cannot determine what to do. `asset_search`,
  `recommend_assets`, and `analyze_scene` are **not** action tools, so a successful
  search/recommendation/scene-summary alone never ends the loop — the loop ends when the model
  inserts (7C/7D), declares a build/edit complete (8A/8B/8D), or otherwise acts.
- **Tool results are bounded.** Each result is compacted before being shown to the model
  (`compact_tool_result`): match lists / children are capped, strings are truncated, and the
  serialized payload has a hard character budget — unbounded hierarchy/property data is never
  exposed to the model (see Project Context below).
- **Every call goes through the existing `ToolRegistry`.** The agent wraps the connection in a
  small `CapturingRBX` so it can observe each `send_request` response without changing any tool,
  validation, or protocol behavior.
- `Agent(provider, registry=..., rbx=..., timeout=..., max_tool_calls=5)` — `max_tool_calls` is
  the per-request bound; it is raised to a bounded 12 when the model calls `build` (Phase 8A).
  `agent_from_env()` builds the agent with the provider configured from the environment
  (defaults to Ollama, see Configuration above).
- **Simple prompts** (e.g. "create a red cube") behave exactly as under Phase 3B: the first
  model reply is a `create_part` or `create_script` call, it executes, and the loop returns —
  one chat call, one tool call. **If the model previously called an inspection tool** (`find_instances`
  or `inspect_instance`) **during the same request**, one optional `inspect_instance`
  verification step is permitted before the loop ends.
- **Verification behavior:**
  - `modify_instance`: after success, exactly one optional `inspect_instance` verification
    step is permitted (existing Phase 4D behavior).
  - `create_part`/`create_script`: after success, an optional `inspect_instance` verification
    step is permitted **only if the model previously called an inspection tool** (`find_instances`
    or `inspect_instance`) during the same request, allowing it to verify the newly-created
    instance against the gathered context. Verification is skipped when the tool result already
    provides sufficient information or when no inspection context was gathered.
  - `insert_asset` (Phase 7D): after success, the Agent **automatically** executes one
    `inspect_instance` call at the reported path and compares path, name, and class. If the
    instance is missing or does not match, the result is `verification_failed` and the loop
    stops without reporting success. The model should not call `inspect_instance` itself after
    `insert_asset`.
  - `build` (Phase 8A) / `plan_build` (Phase 8B): when the model sends a final report after a
    `build` call, the Agent **automatically** executes one `inspect_instance` call for every
    path created during the build and confirms each instance exists. If any build step failed
    or any path cannot be confirmed, the result is `build_failed` and the loop stops without
    reporting a completed build. During build mode the Agent caches `inspect_instance` results
    and reuses them for redundant reads of the same path. If the final report is empty, the
    Agent generates a fallback summary from the recorded created paths so the result always
    explains what was built. The model should not call `inspect_instance` itself to verify the
    build; the system does it.
  - `edit_build` (Phase 8D): when the model sends a final report after an `edit_build` call,
    the Agent **automatically** verifies every modified, newly-created, and deleted path.
    Modified and created paths must exist; deleted paths must be gone. If any edit step failed
    or any path is in the wrong state, the result is `build_failed`. The Agent also generates a
    fallback summary from the recorded changes if the final report is empty. Deletion is
    restricted to objects in the recent build context or touched in the current edit; attempts
    to delete other objects fail with `invalid_deletion`.
- Provider-native tool calling (OpenAI-style `tool_calls`, NIM) is **not** used yet; the loop
  relies on plain `chat` output parsed as JSON (one object per step). Normalizing
  provider-specific tool-call formats behind the provider abstraction is future work.
- **Explicitly out of scope now (future):** long-form conversation history, cross-request memory,
  a full plan → verify → fix cycle, and generalized search/get tools beyond the current
  inspection set.

## Context Management

- Keep the conversation context within practical limits (bounded by the per-request tool-call
  budget and the bounded tool-result compaction in Phase 4D/6C).
- Trim or summarize older turns when needed.
- Tool results must be included in context so the model knows what happened — Phase 4D/6D appends
  a compacted, bounded result after every executed inspection/action call.
- Long-form conversation history / trimming across user requests is **not** yet defined; this is
  an open area.

## Project Context

- The agent loads only **relevant, live** project context — the Phase 4D/6C loop lets the model call
  `find_instances` / `inspect_instance` / `inspect_hierarchy` against the current Workspace and
  acts on the bounded results; Phase 9A adds `analyze_scene` as a higher-level, self-bounding
  entry point for the same information. The agent never dumps the entire project into the prompt
  (decision D-012 preserved).
- Every result is compacted before it is shown to the model: cap on list entries, per-string
  truncation, and a hard serialized-character budget (`MAX_TOOL_RESULT_*` in `cli/agent.py`), so
  unbounded hierarchy/property data never reaches the model.
- Indexing / selective pre-loading of persistent project context remains a long-term, phased
  capability (Phase 4 in [ROADMAP.md](./ROADMAP.md)).

## Failure Handling (Implemented at the provider + agent layers)

- Provider failures (connection, timeout, invalid response) are surfaced as typed errors so the
  agent can retry, degrade, or report:
  - `ProviderConfigError` — missing/invalid configuration
  - `ProviderConnectionError` — endpoint unreachable
  - `ProviderTimeoutError` — request exceeded the timeout
  - `ProviderResponseError` — invalid/unexpected response
  - `ProviderNotImplementedError` — recognized but not implemented (currently `nim`)
- Groq is implemented (Phase 4E) with its own client over the OpenAI-compatible endpoint; failing
  requests surface the same typed errors, and responses missing `choices[0].message.content` are
  rejected as `ProviderResponseError` rather than silently producing empty text.
- The Phase 3B/4D/6C agent converts provider errors into `provider_error` results, malformed model
  output into `malformed_output` results, and registry rejections into `unknown_tool` /
  `invalid_arguments` results — none of these crash the caller. If the loop cannot determine what
  to do (e.g. it exhausts the per-request tool-call budget without completing, code
  `max_tool_calls`), it returns a clear failure instead of guessing.
- The full agent loop (diagnose/fix/verify cycling, see [AGENT.md](./AGENT.md)) beyond the bounded
  Phase 4D/6C loop is **not** implemented yet.
- Exact retry/backoff behavior is not yet defined.

## Open Questions

- Concrete model selection and required tool-calling capabilities for Ollama vs NIM (U-006).
- Whether to adopt provider-native tool calling (e.g. OpenAI-style `tool_calls`) behind the
  provider abstraction instead of JSON-in-text parsing (Phase 3B uses JSON-in-text).
- Context window budget and trimming strategy; conversation history for multi-turn sessions.
- Whether the agent runs inside the CLI process or as a separate service.
- Configuration mechanism and file format (environment variables are the Phase 3A mechanism;
  a config file may be added later).