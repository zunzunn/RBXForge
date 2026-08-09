# RBXForge — AI / Provider Architecture

> **Status:** Provider layer + minimal single-step agent implemented (Phases 3A–3B); the
> multi-step agent loop is not.
>
> - **Implemented (Phase 3A):** a provider-agnostic chat/inference interface in
>   `cli/providers.py`, an **Ollama** backend (local HTTP API), a **mock** backend for tests, and
>   environment-based configuration. NVIDIA **NIM** is recognized by the design (decision D-009)
>   but **not implemented**.
> - **Implemented (Phase 3B):** `cli/agent.py` — a minimal single-step agent that connects the
>   provider layer to the tool layer: natural-language prompt → provider → structured JSON tool
>   call → validation + execution through the `ToolRegistry`.
> - **Not implemented yet:** the multi-step agent loop, conversation history, context management,
>   provider-native tool calling, and project inspection.

## Purpose

RBXForge is **provider-agnostic** (decision D-007). This document defines how the AI/provider
layer works. The model is interchangeable; the rest of the system is not built around any single
model.

```
User prompt
   ↓
Agent  ──►  AI Provider Layer  ──►  Model (Ollama / NIM / future)
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

### Initial Preferred Backend: Ollama (local models) — implemented

- `OllamaProvider` speaks Ollama's local HTTP API (`POST {base_url}/api/chat`); the default base
  URL is `http://127.0.0.1:11434` (no API key needed).
- Runs models locally. Advantages: privacy, offline use, no per-token cost.
- Model name is configurable via `RBXFORGE_MODEL` (never hard-coded).

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
| `RBXFORGE_PROVIDER` | Provider name (`ollama`, `nim`, `mock`)                      | `ollama`             |
| `RBXFORGE_MODEL`    | Model name/identifier for that provider                      | *(required)*         |
| `RBXFORGE_BASE_URL` | Endpoint / base URL (e.g. Ollama server)                     | Ollama default URL   |
| `RBXFORGE_API_KEY`  | API key (auth; never hard-coded in code)                     | *(empty)*            |
| `RBXFORGE_TIMEOUT`  | Request timeout in seconds                                   | `30`                 |

Generation parameters (temperature, max tokens, ...) are passed through provider-specific
`chat()` options (e.g. Ollama's `"options"`).

## Agent / Tool Calling (Implemented — Phase 3B, single step)

> **Implemented (Phase 3B):** `cli/agent.py` connects the provider layer to the Phase 2B tool
> layer. This is a **minimal single-step** agent — no autonomous loop yet. It performs one
> `prompt → provider → tool call → execution` pass and returns.

The flow (`Agent.run(prompt) -> AgentResult`, all failures returned, never raised):

1. **Give the AI the currently registered tool definitions.** The agent serializes
   `registry.list()` into the system prompt — each tool's `name`, `description`, and
   `parameters` (its `input_schema`) — and appends the user prompt. The model is instructed to
   reply with exactly one JSON object: `{"tool": "<name>", "arguments": { ... }}`.
2. **Provider.** `provider.chat(messages)` is called (Ollama in real use, mock in tests). Any
   `ProviderError` (timeout/connection/response/…) becomes a safe `provider_error` result.
3. **Parse.** The reply is parsed into a structured tool call (`parse_tool_call`). The parser
   accepts the JSON object anywhere in the output (including inside a ```json fenced block) and
   uses the first object found. Malformed output (no JSON, non-object JSON, missing/incorrect
   `tool`/`arguments`) becomes a safe `malformed_output` result.
4. **Validate + execute through the `ToolRegistry` only.** `registry.execute(...)` is the sole
   execution path — the same call used by the CLI tool layer. `UnknownToolError` → `unknown_tool`
   result; `InvalidParamsError` → `invalid_arguments` result. Both are rejected before anything
   is sent to the plugin.
5. **Result.** `AgentResult` carries `ok`, the parsed `tool`, the tool layer's `output`, an
   `error` dict (`code`/`message`/`type`), and the raw `provider_text` for diagnostics.

- `Agent(provider, registry=..., rbx=..., timeout=...)` — `registry` defaults to the built-in
  tool registry (`create_part`); `rbx` is the connection object handed to the registry when
  executing a tool (an `RBXForge` instance in real use, a fake in tests).
- `agent_from_env()` builds the agent with the provider configured from the environment (defaults
  to Ollama, see Configuration above).
- Provider-native tool calling (OpenAI-style `tool_calls`, NIM) is **not** used yet: Phase 3B
  relies on plain `chat` output parsed as JSON. Normalizing provider-specific tool-call formats
  behind the provider abstraction is future work.
- **Explicitly out of scope now (future):** multi-step autonomous loops, conversation history,
  plan → tool select → execute → verify cycling, and project inspection.

## Context Management

- Keep the conversation context within practical limits.
- Trim or summarize older turns when needed.
- Tool results must be included in context so the model knows what happened.
- Context handling design is **not** yet defined; this is an open area.

## Project Context

- The agent should load only **relevant** project context where possible (decision D-012).
- Project inspection/indexing (see [ARCHITECTURE.md](./ARCHITECTURE.md)) feeds context
  selectively, rather than dumping the entire project into the prompt.
- This is a long-term, phased capability (Phase 4 in [ROADMAP.md](./ROADMAP.md)).

## Failure Handling (Implemented at the provider + agent layers)

- Provider failures (connection, timeout, invalid response) are surfaced as typed errors so the
  agent can retry, degrade, or report:
  - `ProviderConfigError` — missing/invalid configuration
  - `ProviderConnectionError` — endpoint unreachable
  - `ProviderTimeoutError` — request exceeded the timeout
  - `ProviderResponseError` — invalid/unexpected response
  - `ProviderNotImplementedError` — recognized but not implemented (currently `nim`)
- The Phase 3B agent converts provider errors into `provider_error` results, malformed model
  output into `malformed_output` results, and registry rejections into `unknown_tool` /
  `invalid_arguments` results — none of these crash the caller.
- The agent loop (diagnose/fix, see [AGENT.md](./AGENT.md)) is **not** implemented yet;
  Phase 3B stops at one execution pass.
- Exact retry/backoff behavior is not yet defined.

## Open Questions

- Concrete model selection and required tool-calling capabilities for Ollama vs NIM (U-006).
- Whether to adopt provider-native tool calling (e.g. OpenAI-style `tool_calls`) behind the
  provider abstraction instead of JSON-in-text parsing (Phase 3B uses JSON-in-text).
- Context window budget and trimming strategy; conversation history for multi-turn sessions.
- Whether the agent runs inside the CLI process or as a separate service.
- Configuration mechanism and file format (environment variables are the Phase 3A mechanism;
  a config file may be added later).
