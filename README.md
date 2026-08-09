# RBXForge

AI-powered CLI/agent for building and modifying Roblox games directly inside an open Roblox
Studio project.

## What It Is

Run `rbxforge` and enter an interactive AI session:

```
$ rbxforge
RBXForge > create a small medieval shop
RBXForge > make it medieval
RBXForge > add five swords
```

RBXForge understands your existing project and modifies the currently open Roblox Studio
project — then verifies the change actually works.

## Vision

User prompt → AI agent → inspect project → plan → RBXForge tools → Studio plugin →
Roblox Studio → verify → fix if needed → report.

The long-term goal is complete gameplay features: 3D objects, maps, UI, NPCs, gameplay systems,
Luau scripts, RemoteEvents, data systems, shops, inventories, combat, quests, and more.

## Current Status

**Phase 1 (Studio Connection) implemented.** RBXForge runs a local WebSocket server and the
RBXForge Studio plugin connects to it, announces itself, and answers `ping`/`pong`. No AI, tool
system, or Studio object operations yet.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the phased plan.

## Phase 1 — Try It

Requires Python 3.8+ (standard library only, no install).

**1. Start RBXForge:**

```
./bin/rbxforge
```

It prints `listening on ws://127.0.0.1:7676` and drops into a prompt.

**2. Load the plugin in Roblox Studio:**

Install a real copy into Studio's Plugins directory (Studio skips symlinks, so do not symlink):

```
scripts/install-plugin.sh                 # default Plugins dir
scripts/install-plugin.sh "/Users/you/Documents/Roblox/Plugins"   # your custom "Plugins Dir"
```

Default locations: macOS `~/Library/Application Support/Roblox/Plugins`, Windows
`%LOCALAPPDATA%\Roblox\Plugins`. If you changed Studio's **Plugins Dir** (File → Studio
Settings → Studio → Directories), pass that folder.

Restart Studio, then in Studio: **Plugins** tab → **RBXForge** → **Connect** (allow HTTP
requests for the plugin if prompted). Studio Output should show `[RBXForge] connected to RBXForge
server`.

**3. Verify the connection in the RBXForge prompt:**

```
RBXForge> status      # shows the connected plugin
RBXForge> ping        # RBXForge -> plugin ping, plugin replies pong
RBXForge> quit        # stop RBXForge
```

Non-interactive one-shot (waits for the plugin, pings once, exits):

```
./bin/rbxforge --ping-once --port 7676
```

Automated protocol tests (no Studio needed):

```
python3 tests/test_protocol.py
```

## Planned Architecture

```
AI model → agent → RBXForge tool system → local WebSocket (planned) → Studio plugin → Roblox Studio
```

Provider-agnostic (Ollama initially, NVIDIA NIM supported). Tools, not arbitrary mutation.
Everything flows through a dedicated Studio plugin. The local WebSocket connection to the plugin
is implemented (Phase 1); the AI/tool layers are planned.

## Development Approach

Small, verified milestones. One focused capability at a time; verify before moving on.

## Documentation

- [docs/PROJECT.md](docs/PROJECT.md) — project source of truth
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — intended architecture
- [docs/ROADMAP.md](docs/ROADMAP.md) — phased roadmap
- [docs/DECISIONS.md](docs/DECISIONS.md) — architecture decisions
- [docs/AGENT.md](docs/AGENT.md) — agent behavior
- [docs/TOOLS.md](docs/TOOLS.md) — planned tool system
- [docs/PLUGIN.md](docs/PLUGIN.md) — planned Studio plugin
- [docs/AI.md](docs/AI.md) — AI provider architecture
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — communication protocol (draft)
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — development rules
