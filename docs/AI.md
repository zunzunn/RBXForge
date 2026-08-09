# RBXForge — AI / Provider Architecture

> **Status:** Provider layer implemented (Phase 3A); the agent loop is not.
>
> - **Implemented (Phase 3A):** a provider-agnostic chat/inference interface in
>   `cli/providers.py`, an **Ollama** backend (local HTTP API), a **mock** backend for tests, and
>   environment-based configuration. NVIDIA **NIM** is recognized by the design (decision D-009)
>   but **not implemented**.
> - **Not implemented yet:** the agent loop, tool calling, context management, and natural-language
>   → tool parsing.

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

## Agent / Tool Calling

> **Not implemented yet (future).** Phase 3A deliberately does **not** connect AI to Roblox tools.

- The provider must eventually support the agent calling RBXForge tools (see [TOOLS.md](./TOOLS.md)).
- The agent flow: model produces a plan and tool calls → tool system executes → results feed
  back to the model → loop continues until done.
- Provider-specific tool-calling formats must be normalized behind the provider abstraction so
  the agent logic stays provider-independent.
- Model capability for reliable tool calling is a practical constraint; which models to target
  is an open decision (U-006).

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

## Failure Handling (Implemented at the provider layer)

- Provider failures (connection, timeout, invalid response) are surfaced as typed errors so the
  agent can retry, degrade, or report:
  - `ProviderConfigError` — missing/invalid configuration
  - `ProviderConnectionError` — endpoint unreachable
  - `ProviderTimeoutError` — request exceeded the timeout
  - `ProviderResponseError` — invalid/unexpected response
  - `ProviderNotImplementedError` — recognized but not implemented (currently `nim`)
- The agent loop already handles operation failures via diagnose/fix (see [AGENT.md](./AGENT.md));
  provider-layer failures are a distinct class the agent must also handle gracefully.
- Exact retry/backoff behavior is not yet defined.

## Open Questions

- Concrete model selection and required tool-calling capabilities for Ollama vs NIM (U-006).
- Context window budget and trimming strategy.
- Whether the agent runs inside the CLI process or as a separate service.
- Configuration mechanism and file format (environment variables are the Phase 3A mechanism;
  a config file may be added later).
