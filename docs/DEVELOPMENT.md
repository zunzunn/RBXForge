# RBXForge — Development Rules

> **Status:** Current. This file is the main instruction document for future coding agents.

These rules apply to any agent or developer modifying the RBXForge repository.

## The Rules

1. **Read the relevant `/docs` files before modifying the project.** Start with
   [PROJECT.md](./PROJECT.md) and [DECISIONS.md](./DECISIONS.md), then the file relevant to the
   task ([ARCHITECTURE.md](./ARCHITECTURE.md), [ROADMAP.md](./ROADMAP.md),
   [AGENT.md](./AGENT.md), [TOOLS.md](./TOOLS.md), [PLUGIN.md](./PLUGIN.md),
   [AI.md](./AI.md), [PROTOCOL.md](./PROTOCOL.md)).
2. **Do not implement multiple major features at once.** One focused capability at a time.
3. **Work in small milestones.** Each milestone implements one focused capability
   (see [ROADMAP.md](./ROADMAP.md)).RBXForge — Step 1: Establish Project Documentation

   We are building RBXForge, an AI-powered CLI/agent for building and modifying Roblox games directly inside an open Roblox Studio project.

   You are working as the primary software architect/developer for this project.

   IMPORTANT WORKING RULE

   We are building RBXForge incrementally.

   Do NOT implement the application yet.

   This step is documentation and project-definition only.

   Do not create the CLI, AI agent, Roblox Studio plugin, WebSocket server, or any implementation code yet.

   First establish a clean, authoritative project context that future AI agents can read before making changes.

   ⸻

   Product Vision

   The user launches:

   rbxforge

   and enters an interactive AI session:

   RBXForge > add a shop
   RBXForge > make it medieval
   RBXForge > add five swords

   RBXForge should understand the user’s existing Roblox project and modify the currently open Roblox Studio project.

   The long-term goal is:

   User prompt
   ↓
   RBXForge AI Agent
   ↓
   Inspect relevant existing project
   ↓
   Plan
   ↓
   Use RBXForge tools
   ↓
   RBXForge Studio Plugin
   ↓
   Roblox Studio
   ↓
   Verify result
   ↓
   Fix if necessary
   ↓
   Report completion

   The long-term product should eventually be capable of building complete gameplay features, including:

   - 3D objects/models
   - maps
   - UI
   - NPCs
   - gameplay systems
   - Luau scripts
   - RemoteEvents/RemoteFunctions
   - data systems
   - shops
   - inventories
   - combat
   - quests
   - etc.

   However, the first version should focus on building/modifying Studio objects, with gameplay logic added later.

   ⸻

   Decisions Already Made

   Record these decisions as authoritative unless explicitly changed later.

   Product

   Name:

   RBXForge

   Primary command:

   rbxforge

   Primary interaction:

   Interactive AI agent

   Example:

   $ rbxforge
   RBXForge > create a small medieval shop

   ⸻

   Autonomy

   RBXForge should execute changes automatically.

   It should NOT ask for confirmation for every normal operation.

   The intended agent behavior is:

   Understand
   → Inspect
   → Plan
   → Execute
   → Verify
   → Fix if necessary
   → Verify again
   → Report

   ⸻

   Roblox Studio Integration

   RBXForge will communicate with a dedicated Roblox Studio plugin.

   The plugin is the bridge between RBXForge and Roblox Studio.

   Initial architecture:

   RBXForge CLI
   ↓
   AI Agent
   ↓
   Local communication
   ↓
   RBXForge Studio Plugin
   ↓
   Roblox Studio

   A local WebSocket-style connection is the current preferred communication approach, but do not implement it yet.

   ⸻

   AI Architecture

   RBXForge should be provider-agnostic.

   The initial preferred AI backend is:

   Ollama / local models

   NVIDIA NIM should be supported as another possible backend.

   The architecture should eventually allow additional providers without rewriting the agent.

   The AI model itself is NOT the core of RBXForge.

   The important architecture is:

   AI Model
   ↓
   Agent
   ↓
   RBXForge Tool System
   ↓
   Studio Plugin
   ↓
   Roblox Studio

   ⸻

   Tool-Based Architecture

   The AI should NOT directly manipulate Roblox arbitrarily.

   Instead, RBXForge should provide Roblox-specific tools.

   Examples:

   inspect_project
   search_instances
   create_part
   create_model
   create_folder
   modify_instance
   move_object
   rotate_object
   scale_object
   create_ui
   delete_instance
   create_script
   run_luau
   verify

   The actual tool list will be designed later.

   ⸻

   Project Awareness

   RBXForge should understand the existing Roblox project.

   It should NOT blindly create duplicate systems.

   The long-term architecture should use intelligent project inspection/indexing.

   For example:

   User:

   > add a shop near the town

   RBXForge should be able to:

   Find Town
   Find relevant existing systems
   Inspect nearby objects
   Check existing shop/currency/inventory systems
   Plan around the existing architecture

   It should only load relevant project context into the AI whenever possible.

   ⸻

   Agent Loop

   RBXForge should eventually operate as a full agent loop:

   PROMPT
   ↓
   UNDERSTAND
   ↓
   INSPECT
   ↓
   PLAN
   ↓
   EXECUTE
   ↓
   VERIFY
   ↓
   SUCCESS?
   ┌─┴─┐
   YES NO
   ↓ ↓
   DONE DIAGNOSE
   ↓
   FIX
   ↓
   VERIFY

   The goal is not merely to generate code.

   The goal is:

   Make the requested change actually work in Roblox Studio.

   ⸻

   Development Strategy

   RBXForge will be developed through small milestones.

   Every milestone must follow:

   1. Implement one focused capability
   2. Run/verify it
   3. Inspect failures
   4. Fix failures
   5. Re-run verification
   6. Only then move to the next milestone

   Do not combine multiple major milestones into one implementation.

   ⸻

   First Technical Milestone

   The first actual functionality we will eventually build should be extremely small:

   RBXForge > create a red cube

   The cube should actually appear inside the currently open Roblox Studio project.

   After that we can progressively test:

   create a red cube
   ↓
   modify the cube
   ↓
   move the cube
   ↓
   create multiple objects
   ↓
   create a simple structure
   ↓
   create a small shop
   ↓
   add UI
   ↓
   add gameplay logic

   Do NOT implement this milestone yet.

   ⸻

   Documentation To Create

   Create a /docs directory and create the following Markdown files.

   1. docs/PROJECT.md

   This is the high-level source of truth for RBXForge.

   Include:

   - What RBXForge is
   - Product vision
   - Target user
   - Core use case
   - Example user interactions
   - Long-term goal
   - Current development stage
   - What RBXForge is NOT
   - Important architectural principles

   ⸻

   2. docs/ARCHITECTURE.md

   Document the intended architecture.

   Include diagrams using Mermaid where useful.

   Cover:

   - CLI
   - interactive agent
   - AI provider layer
   - agent loop
   - tool system
   - project inspection/index
   - local communication layer
   - Roblox Studio plugin
   - Roblox Studio
   - future verification system

   Clearly distinguish:

   planned architecture vs currently implemented architecture.

   At this point everything is planned, because implementation has not started.

   ⸻

   3. docs/ROADMAP.md

   Create a staged roadmap.

   Do NOT make unrealistic claims.

   Organize it approximately like:

   Phase 0 — Project Definition

   Documentation and architecture.

   Phase 1 — Studio Connection

   RBXForge can communicate with the Roblox Studio plugin.

   Phase 2 — Basic Studio Tools

   Create/modify simple Roblox Instances.

   Phase 3 — First Agent Loop

   Prompt → tool selection → execution → verification.

   Phase 4 — Project Awareness

   Inspect and understand the existing project.

   Phase 5 — Building Systems

   Models, structures, UI, etc.

   Phase 6 — Gameplay Logic

   Luau and complete gameplay features.

   Phase 7 — Autonomous Game Development

   More advanced planning, debugging, and multi-step feature construction.

   For each phase include:

   - Goal
   - Deliverables
   - Verification criteria
   - Dependencies
   - What is explicitly NOT included

   Do not assign arbitrary dates.

   ⸻

   4. docs/DECISIONS.md

   Create an Architecture Decision Record-style document.

   Record every decision already made above.

   For each decision include:

   - Decision
   - Status
   - Reason
   - Alternatives considered
   - Consequences

   Clearly mark uncertain decisions as provisional rather than pretending they are final.

   ⸻

   5. docs/AGENT.md

   Define how the RBXForge AI agent is expected to behave.

   Document:

   - Understand before acting
   - Inspect relevant project context
   - Plan before execution
   - Use RBXForge tools
   - Avoid unnecessary changes
   - Verify results
   - Diagnose failures
   - Attempt fixes
   - Verify fixes
   - Report exactly what changed

   Also document that the agent should preserve existing project functionality whenever possible.

   ⸻

   6. docs/TOOLS.md

   Document the planned RBXForge tool system.

   Start with conceptual tools only.

   For example:

   inspect_project
   search_instances
   get_instance
   create_instance
   modify_instance
   delete_instance
   move_instance
   create_script
   modify_script
   create_ui
   run_luau
   verify

   For each tool explain:

   - Purpose
   - Inputs conceptually
   - Expected output
   - Why the agent might use it

   Do NOT implement the tools yet.

   Do NOT invent final APIs yet.

   ⸻

   7. docs/PLUGIN.md

   Document the planned Roblox Studio plugin.

   Include:

   - Purpose
   - Responsibilities
   - Relationship to RBXForge
   - How it is expected to communicate with RBXForge
   - What the plugin should eventually be capable of
   - Security considerations
   - Error handling considerations

   Do not implement the plugin yet.

   ⸻

   8. docs/AI.md

   Document the AI/provider architecture.

   Include:

   - Provider abstraction
   - Ollama as the initial preferred backend
   - NVIDIA NIM as an optional backend
   - Future provider support
   - Model configuration
   - Agent/tool calling
   - Context management
   - Project context
   - Failure handling

   Do not hard-code a specific model yet.

   ⸻

   9. docs/PROTOCOL.md

   Document the planned communication protocol between:

   RBXForge
   ↕
   Studio Plugin

   This should initially be conceptual.

   Describe:

   - Request
   - Response
   - Tool execution
   - Success
   - Failure
   - Errors
   - Request IDs
   - Future streaming/events

   Do not implement WebSocket communication yet.

   Clearly mark the protocol as Draft.

   ⸻

   10. docs/DEVELOPMENT.md

   Document development rules for future AI agents working on RBXForge.

   Include these rules:

   1. Read the relevant /docs files before modifying the project.
   2. Do not implement multiple major features at once.
   3. Work in small milestones.
   4. Verify every milestone before proceeding.
   5. Fix failures before moving forward.
   6. Do not silently change architectural decisions.
   7. Update documentation when architectural decisions change.
   8. Do not introduce unnecessary dependencies.
   9. Prefer simple implementations.
   10. Never claim something works without verification.

   This file should become the main instruction document for future coding agents.

   ⸻

   Root README

   Also create:

   README.md

   It should be short and explain:

   - What RBXForge is
   - The vision
   - Example usage
   - Current status
   - Planned architecture
   - Development approach

   Do not write a huge README.

   ⸻

   Important Documentation Rule

   Do not pretend that planned features already exist.

   Use language such as:

   Planned
   Proposed
   Future
   Not yet implemented

   where appropriate.

   The documentation must clearly distinguish:

   CURRENT
   PLANNED
   FUTURE

   ⸻

   Verification

   After creating the files:

   1. List every file created.
   2. Verify every required Markdown file exists.
   3. Check that the documents do not contradict each other.
   4. Check that the architecture reflects the decisions above.
   5. Check that no implementation code was added.
   6. Check that no unnecessary dependencies were installed.
   7. Identify any unresolved architectural questions that must be decided before implementation.

   Do NOT start Step 2.

   Stop after documentation is complete and verified.

   At the end, provide:

   STEP 1 COMPLETE
   Files created:
   ...
   Implementation:
   None
   Verification:
   ...
   Unresolved decisions:
   ...

   Wait for further instructions.

4. **Verify every milestone before proceeding.** Run it, observe the result, and confirm it
   actually works.
5. **Fix failures before moving forward.** Inspect the failure, fix it, re-run verification,
   and only then move on.
6. **Do not silently change architectural decisions.** Recorded decisions live in
   [DECISIONS.md](./DECISIONS.md). If a decision must change, update the decision record first.
7. **Update documentation when architectural decisions change.** Docs are the source of truth;
   keep them consistent with the code and with each other.
8. **Do not introduce unnecessary dependencies.** Prefer the standard library and simple
   solutions.
9. **Prefer simple implementations.** Favor clarity and minimalism over cleverness.
10. **Never claim something works without verification.** A claim of "works" must be backed by
    an actual run/verification.

## Working Rhythm

For every milestone:

1. Pick the next item from the roadmap.
2. Read the relevant docs.
3. Implement one focused capability.
4. Run / verify it.
5. Inspect failures.
6. Fix failures.
7. Re-run verification.
8. Move on.

## Cross-Cutting Constraints

- **No implementation yet in Phase 0.** Until Phase 1 begins, the project is documentation-only.
  Do not add CLI, agent, plugin, WebSocket, or tool code without explicit instruction.
- **The AI model is not the core.** The tool system, plugin, protocol, verification, and project
  awareness carry the value (decision D-010).
- **Changes flow through the plugin.** No component other than the plugin touches Roblox Studio
  directly (decision D-005).
- **Tools, not arbitrary mutation.** The agent only manipulates Roblox through RBXForge tools
  (decision D-011).
- **Autonomy.** The agent executes without per-operation confirmation (decision D-004); safety
  is handled inside the tool system, not by blocking prompts.

## Status Markers

Use these consistently in documentation:

| Marker         | Meaning                                          |
| -------------- | ------------------------------------------------ |
| Current / Done | Exists and is verified now                       |
| Planned        | Intended, designed conceptually, not implemented |
| Future         | Intended long-term, not designed                 |
| Draft          | Initial, will change                             |

Never describe planned features as already existing.
