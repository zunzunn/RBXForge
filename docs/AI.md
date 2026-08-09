# RBXForge — AI / Provider Architecture

> **Status:** Planned. No AI code exists. No specific model is hard-coded.

## Purpose

RBXForge is **provider-agnostic** (decision D-007). This document defines how the AI/provider
layer should work. The model is interchangeable; the rest of the system is not built around any
single model.

```
User prompt
   ↓
Agent  ──►  AI Provider Layer  ──►  Model (Ollama / NIM / future)
                ▲
                │ model output (tool calls, text)
   Agent uses RBXForge tools
```

## Provider Abstraction

- The agent depends on a **stable provider interface**, not on a specific provider.
- Providers plug into this interface.
- The interface must expose the operations the agent needs, notably **agent/tool calling**
  (see below).
- Adding a provider must not require rewriting the agent.

### Initial Preferred Backend: Ollama (local models)

- Runs models locally.
- Advantages: privacy, offline use, no per-token cost.
- RBXForge should work well with Ollama first (decision D-008).

### Optional Backend: NVIDIA NIM

- Supported as another possible backend (decision D-009).
- Fits behind the same provider abstraction as Ollama.

### Future Providers

- The abstraction should allow additional providers (hosted APIs, other local runners, etc.)
  without rewriting the agent.

## Model Configuration

- No specific model is hard-coded.
- Configuration should include:
  - Provider choice.
  - Model name/identifier for that provider.
  - Connection details (endpoint, base URL, auth if required).
  - Generation parameters (temperature, max tokens, etc.).
- Configuration should be user-adjustable (planned: a config mechanism, not yet designed).

## Agent / Tool Calling

- The provider must support the agent calling RBXForge tools (see [TOOLS.md](./TOOLS.md)).
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

## Failure Handling

- Provider failures (connection, timeout, invalid response, malformed tool calls) must be
  surfaced to the agent so it can retry, degrade, or report.
- The agent loop already handles operation failures via diagnose/fix (see
  [AGENT.md](./AGENT.md)); provider-layer failures are a distinct class the agent must also
  handle gracefully.
- Exact retry/backoff behavior is not yet defined.

## Open Questions

- Concrete model selection and required tool-calling capabilities for Ollama vs NIM (U-006).
- Context window budget and trimming strategy.
- Whether the agent runs inside the CLI process or as a separate service.
- Configuration mechanism and file format.
