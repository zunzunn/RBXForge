# RBXForge — Communication Protocol

> **Status:** Partially implemented.
>
> - **Current / Implemented (Phase 2B):** the transport and the message types below are
>   implemented and verified — the CLI side in `cli/rbxforge.py`, the plugin side in
>   `plugin/rbxforge.lua`. One Studio operation (`create_part`) is implemented as a registered
>   tool, dispatched through registries on both sides, with CLI-side argument validation before
>   any `request` is sent.
> - **Planned / Future:** additional tool execution, streaming, and plugin-initiated events
>   are not implemented yet.

## Purpose

Defines communication between RBXForge and the Roblox Studio plugin.

```
RBXForge CLI  ↕  (local WebSocket)  ↕  Studio Plugin
```

## Principles

- Local-only communication (developer's machine).
- Request/response model with **request IDs** so responses can be matched to requests.
- Every request gets a response: success **or** failure — never silence.
- The plugin is the executor; RBXForge is the caller. In this milestone RBXForge is also the
  server; the plugin is the client.

## Transport (Implemented)

- **Type:** WebSocket over TCP, bound to `127.0.0.1` only (not exposed to the network).
- **Default URL:** `ws://127.0.0.1:7676` (override with `rbxforge --port <port>`; the plugin's
  `DEFAULT_URL` constant must match).
- **Roles:** RBXForge CLI runs the WebSocket **server**; the Studio plugin is the **client**.
- **Studio side:** uses `HttpService:CreateWebStreamClient(Enum.WebStreamClientType.WebSocket,
  { Url = ... })`. Available in Roblox Studio only (not in live experiences).
- Exactly one active plugin connection at a time. If a second plugin connects, RBXForge drops
  the previous connection.
- If the plugin's WebSocket client cannot be created (older Studio without WebSocket support),
  the plugin logs a clear error. This is the known fallback/risk area (decision U-002).

## Wire Format (Implemented)

Every message is a single JSON object with this envelope:

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | Message type (below). |
| `id` | string \| null | Request ID for pairing; `null` for notifications. |
| `version` | number | Protocol version. Current value: `1`. |
| `timestamp` | number | Unix seconds when the message was created (for logging). |
| `payload` | object | Type-specific data (always an object). |

## Message Types (Implemented)

### `hello` — plugin → RBXForge

Sent by the plugin immediately after the WebSocket opens. Lets RBXForge detect the plugin.

```json
{
  "type": "hello",
  "id": null,
  "version": 1,
  "timestamp": 0.0,
  "payload": { "name": "rbxforge-plugin", "version": "0.1.0", "protocol": 1 }
}
```

### `welcome` — RBXForge → plugin

RBXForge's reply to `hello`, acknowledging the connection.

```json
{
  "type": "welcome",
  "id": null,
  "version": 1,
  "timestamp": 0.0,
  "payload": { "name": "rbxforge", "version": "0.1.0", "protocol": 1 }
}
```

### `ping` — RBXForge → plugin

The single test message implemented in Phase 1. Carries a unique `id`.

```json
{
  "type": "ping",
  "id": "ping-1",
  "version": 1,
  "timestamp": 0.0,
  "payload": { "message": "ping", "timestamp": 0.0 }
}
```

### `pong` — plugin → RBXForge

The plugin's reply to a `ping`. Must echo the same `id`. RBXForge matches `id` to the
outstanding ping and reports round-trip time.

```json
{
  "type": "pong",
  "id": "ping-1",
  "version": 1,
  "timestamp": 0.0,
  "payload": { "message": "pong" }
}
```

### `bye` — plugin → RBXForge (optional)

Sent by the plugin when it disconnects deliberately. RBXForge logs it; no reply.

```json
{
  "type": "bye",
  "id": null,
  "version": 1,
  "timestamp": 0.0,
  "payload": { "reason": "user requested disconnect" }
}
```

### `request` — RBXForge → plugin

Carries one tool call. `payload.tool` names the operation; `payload.params` holds its
arguments. Always carries a unique `id` that the plugin must echo in its `response`.

```json
{
  "type": "request",
  "id": "req-1",
  "version": 1,
  "timestamp": 0.0,
  "payload": {
    "tool": "create_part",
    "params": {
      "name": "RBXForgeTestPart",
      "position": { "x": 0, "y": 5, "z": 0 },
      "size": { "x": 4, "y": 4, "z": 4 },
      "color": "red"
    }
  }
}
```

### `response` — plugin → RBXForge

The plugin's reply to a `request`. Echoes the request's `id`. On success
`payload.ok` is `true` and `payload.result` holds the result; on failure `payload.ok` is
`false` and `payload.error` holds `{ "code", "message" }`.

```json
{
  "type": "response",
  "id": "req-1",
  "version": 1,
  "timestamp": 0.0,
  "payload": {
    "ok": true,
    "result": {
      "name": "RBXForgeTestPart",
      "position": { "x": 0, "y": 5, "z": 0 },
      "size": { "x": 4, "y": 4, "z": 4 },
      "color": "red"
    }
  }
}
```

A failure response:

```json
{
  "type": "response",
  "id": "req-1",
  "version": 1,
  "timestamp": 0.0,
  "payload": {
    "ok": false,
    "error": { "code": "invalid_params", "message": "unsupported color: blue" }
  }
}
```

Implemented tools and their `params`:

| Tool | Params | Result |
| --- | --- | --- |
| `create_part` | `name` (string), `position` / `size` (object with numeric `x`, `y`, `z`), `color` (string; `"red"` supported) | `{ name, position, size, color }` |

Tool error codes (in `response.payload.error.code`):

| Code | Meaning |
| --- | --- |
| `invalid_params` | A `params` value failed validation (missing/wrong type/unsupported value). |
| `unknown_tool` | `payload.tool` is not implemented by the plugin. |
| `execution_failed` | The operation ran but failed in Studio (e.g. could not parent to workspace). |

### `error` — either direction

Sent when a message cannot be handled. `payload.code` is machine-readable;
`payload.message` is human-readable.

```json
{
  "type": "error",
  "id": null,
  "version": 1,
  "timestamp": 0.0,
  "payload": { "code": "malformed_message", "message": "message is not valid JSON" }
}
```

Implemented error codes:

| Code | Meaning |
| --- | --- |
| `malformed_message` | The message was not valid JSON or not a JSON object. |
| `unknown_message_type` | `type` was not recognized. |

## Request IDs

- Every `ping` and `request` carries a unique `id` (`ping-1`, `req-1`, ...).
- The matching `pong` / `response` echoes that `id`, letting RBXForge match replies to
  requests.
- `hello`, `welcome`, and `bye` are notifications (`id: null`).

## Connection Lifecycle (Implemented)

1. Plugin connects (TCP + WebSocket handshake). RBXForge logs the client address.
2. Plugin sends `hello`; RBXForge logs `PLUGIN CONNECTED` and replies `welcome`.
3. RBXForge may send `ping` (plugin replies `pong`) or a tool `request` (plugin replies
   `response`).
4. On close (plugin quits, user disconnects, or network drops), RBXForge logs
   `PLUGIN DISCONNECTED` and forgets the connection. The server keeps running.
5. RBXForge terminates cleanly on `quit` (it sends no frames; TCP close is the signal).

## Tool Execution (Implemented — Phase 2B)

Each RBXForge tool call maps to one `request` message (see [TOOLS.md](./TOOLS.md)); the tool
`params` are defined in "Implemented tools and their `params`" above. Before any `request` is
sent, the CLI **validates the arguments against the tool's input schema**
(cli/rbxforge.py `ToolRegistry`). Invalid arguments are rejected locally — no `request` is sent —
and reported to the caller. On the plugin side, incoming `request`s are dispatched through a
**tool-handler registry** (`plugin/rbxforge.lua` `toolHandlers` / `registerTool`), not a
hard-coded branch. Exactly one tool is implemented: `create_part` (creates a Part in `workspace`
with the given name, position, size, and color). Every `request` gets a `response` — never
silence; the CLI-side validation failure replaces the request/response round trip with a local
rejection before anything is sent.

## Future Streaming / Events (Planned)

Not implemented; listed as future direction:

- Streaming status updates for long operations.
- Plugin-initiated events (project changed, errors).
- Cancellation of in-flight requests.
- Heartbeats / liveness checks.

## Non-Goals (for this milestone)

- Only one Studio operation (`create_part`). Other object operations are planned.
- No authentication/encryption (connection is local only).
- No binary frames (rejected with a log line and ignored).
