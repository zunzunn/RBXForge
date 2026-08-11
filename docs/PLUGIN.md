# RBXForge — Studio Plugin

> **Status:** Phases 1–2 + Phases 4A–4C implemented in `plugin/rbxforge.lua`. It connects to the
> local RBXForge process over WebSocket, announces itself, answers `ping`/`pong`, and executes
> the `create_part` (Phase 2), `inspect_hierarchy` (Phase 4A), `find_instances` (Phase 4B), and
> `inspect_instance` (Phase 4C) tools (with tool-handler dispatch). Additional Studio
> operations are **not** implemented yet.

## Purpose

The RBXForge Studio Plugin is the bridge between RBXForge and Roblox Studio. It is the only
component that operates directly on the currently open Studio project. See decision D-005 in
[DECISIONS.md](./DECISIONS.md).

```
RBXForge CLI
      ↓
AI Agent
      ↓
Local communication (WebSocket-style, planned)
      ↓
RBXForge Studio Plugin
      ↓
Roblox Studio
```

## Responsibilities

- Receive requests from RBXForge over the local communication channel.
- Interpret each request into Studio operations (creating, modifying, deleting instances, etc.).
- Perform the operations on the currently open project.
- Return results, including success/failure and any data, back to RBXForge.
- Provide access to the project structure for inspection/search tools.
- Provide a mechanism to run Luau in the project context (for `run_luau` and verification).
- Surface errors clearly so RBXForge can diagnose and fix.

## Relationship to RBXForge

- The plugin is **subordinate**: it executes requests and returns results. It does not make
  decisions.
- All decisions come from the RBXForge agent; all changes flow through the plugin.
- The plugin and RBXForge agree on a communication protocol (see
  [PROTOCOL.md](./PROTOCOL.md), currently Draft).

## Communication (Implemented)

- **Transport:** local WebSocket connection (decision D-006, now proven). The plugin uses
  `HttpService:CreateWebStreamClient(Enum.WebStreamClientType.WebSocket, { Url = "ws://127.0.0.1:7676" })`.
  WebSockets in Studio are supported by current versions of Roblox Studio (announced 2025).
- **Protocol:** request/response messages with request IDs (see [PROTOCOL.md](./PROTOCOL.md)).
  Phase 1 implements `hello`, `welcome`, `ping`, `pong`, `bye`, `error`.
- The connection is local to the developer's machine; no external server or deployment.

## Current Implementation (Phase 1)

- Toolbar with **Connect** / **Disconnect** / **Status** buttons in the "RBXForge" plugin tab.
- On **Connect**: creates the WebStreamClient, connects to the local RBXForge process, and sends
  `hello` on open. Logs to Studio Output with the `[RBXForge]` prefix.
- On receiving `ping`: replies with `pong` echoing the request ID.
- On **Disconnect** or connection close: sends `bye` (if deliberate), cleans up, and logs.
- If the WebSocket API is unavailable, logs a clear error telling the user to update Studio and
  allow HTTP requests for the plugin.

### Loading the plugin

Studio loads local plugins from the **Plugins** directory as regular files (`.lua` scripts or
`.rbxm` models). It **skips symlinks**, so install a real copy — do not symlink into the repo.

Install with:

```
scripts/install-plugin.sh                       # default Studio Plugins dir
scripts/install-plugin.sh /path/to/your/Plugins # your "Plugins Dir" from Studio Settings
```

or copy the file manually:

- macOS default: `~/Library/Application Support/Roblox/Plugins/`
- Windows default: `%LOCALAPPDATA%\Roblox\Plugins\`

If you changed Studio's **Plugins Dir** (File → Studio Settings → Studio → Directories), install
into *that* folder instead.

Then restart Studio (or reload via **PluginDebugService**). In Studio: **Plugins** tab →
**RBXForge** → **Connect**. If prompted, allow HTTP requests for the plugin. Start the RBXForge
CLI first (see the repo README). With **Plugin Debugging Enabled**, the plugin script also
appears under `PluginDebugService` for reload/debug.

## Capabilities the Plugin Should Eventually Support

- Inspect the project tree.
- Search/find instances.
- Get instance details.
- Create, modify, delete instances.
- Move / rotate / scale instances.
- Create and edit scripts (Luau source).
- Create UI elements.
- Run Luau in the project context.
- Verify changes (existence, property values, script behavior).

Capabilities arrive incrementally with the roadmap (see [ROADMAP.md](./ROADMAP.md)).

## Security Considerations

- The plugin performs **real changes** to the user's project. The user is expected to have
  Roblox Studio open on the project they intend to modify.
- Malicious or malformed requests must be handled safely: validate input, bound sizes, and never
  execute untrusted Luau without guardrails.
- `run_luau` is powerful and potentially destructive; it must be carefully constrained and
  reviewed before implementation.
- The local connection should not be exposed to the network.
- The plugin should surface confirmation/context about destructive operations where feasible,
  even though the agent operates autonomously (see U-001 in
  [DECISIONS.md](./DECISIONS.md)).

## Error Handling Considerations

- Every operation should return a clear success/failure response (see
  [PROTOCOL.md](./PROTOCOL.md)).
- Errors should be categorized so the agent can diagnose (e.g. instance not found, invalid
  property, script error, transport error).
- The plugin must not crash Studio on error; failures should be contained and reported.
- If the connection drops mid-request, the plugin should handle the orphaned request gracefully.
- Operations that partially succeed should be reported as such, not as silent success.

## Out of Scope (for now)

- Any gameplay logic decisions.
- Any AI behavior — the plugin only executes.
- Any changes without a corresponding request from RBXForge.
