---
name: invoking-cli-agents
description: Use when needing to programmatically invoke a CLI coding agent (Claude Code, OpenCode) from Python, delegate work to a sub-agent, or orchestrate multiple agents. Covers AgentShell instantiation, prompt execution, response streaming, session resumption, tool scoping, and cost tracking.
---

# Invoking CLI Agents with AgentShell

AgentShell is a Python library that runs CLI coding agents headlessly and returns structured output. It hides agent-specific CLI differences behind a unified interface so your code works regardless of which agent runs underneath.

## When to Use

- You need to invoke Claude Code, OpenCode, or another CLI agent from Python
- You want to delegate a coding task to a sub-agent and collect the result
- You need to orchestrate multi-step workflows across agents
- You want to stream agent output in real-time

## When NOT to Use

- You want to call the Anthropic API directly (use the SDK instead)
- The CLI agent is already running interactively and you just need its output

## Installation

```bash
uv add agent-shell-py
```

## Core Concepts

AgentShell has two methods: `execute()` for collecting a complete response, and `stream()` for real-time event processing. Both are async.

### Execute: Run and Collect

Use when you want the final answer and don't need intermediate output.

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Analyse the authentication module and list all public functions",
    allowed_tools=["Read", "Glob", "Grep"],
    model="sonnet",
)

print(response.response)      # Full text output
print(f"Cost: ${response.cost:.4f}")
print(f"Session: {response.session_id}")
```

### Stream: Real-Time Events

Use when you need progress feedback, want to display output incrementally, or need to react to specific event types (tool use, thinking, errors).

```python
async for event in shell.stream(
    cwd="/path/to/project",
    prompt="Refactor the auth module to use dependency injection",
    allowed_tools=["Read", "Edit", "Bash"],
    model="sonnet",
    effort="high",
    include_thinking=True,
):
    if event.type == "system":
        print(f"Session: {event.session_id}")
    elif event.type == "thinking":
        print(f"[thinking] {event.content}")
    elif event.type == "tool_use":
        print(f"[tool] {event.content}")
    elif event.type == "text":
        print(event.content)
    elif event.type == "result":
        print(f"Done. Cost: ${event.cost:.4f}, Duration: {event.duration:.1f}s")
```

### Session Resumption

Pass `session_id` from a previous response to continue the conversation. This enables multi-turn workflows where each step builds on the last.

```python
# Step 1: Analyse
analysis = await shell.execute(
    cwd="/path/to/project",
    prompt="Analyse this codebase and identify areas that need refactoring",
    allowed_tools=["Read", "Glob", "Grep"],
    model="sonnet",
)

# Step 2: Act on the analysis (same session)
refactor = await shell.execute(
    cwd="/path/to/project",
    prompt="Now refactor the top priority item you identified",
    allowed_tools=["Read", "Edit", "Bash"],
    model="sonnet",
    session_id=analysis.session_id,
)
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `cwd` | `str` | required | Working directory (must exist) |
| `prompt` | `str` | required | Task or question for the agent |
| `allowed_tools` | `list[str] \| None` | `None` | Restrict which tools the agent can use. `None` = all tools. **Claude Code only.** |
| `model` | `str \| None` | `None` | Model alias or full name (e.g. `"sonnet"`, `"claude-sonnet-4-6"`) |
| `effort` | `str \| None` | `None` | Reasoning effort: `"low"`, `"medium"`, `"high"`, `"max"`. **Claude Code only.** |
| `include_thinking` | `bool` | `False` | Filter only: yields thinking events in `stream()` if already present in CLI output. Does not add a CLI flag. **Claude Code `stream()` only. Dropped by `execute()`.** |
| `auto_approve` | `bool` | `True` | Skip tool permission prompts. **Claude Code only.** |
| `session_id` | `str \| None` | `None` | Resume a previous session |

> **Agent parity warning:** OpenCode currently only maps `model` and `session_id` to CLI flags. Parameters like `allowed_tools`, `effort`, `include_thinking`, and `auto_approve` are accepted but silently ignored. Do not rely on `allowed_tools` for safety when using OpenCode.

## Supported Agents

```python
from agent_shell.models.agent import AgentType

AgentType.CLAUDE_CODE   # Claude Code CLI
AgentType.OPENCODE      # OpenCode CLI
```

Gemini CLI, Copilot CLI, and Codex have enum values but no adapter yet.

## Tool Scoping (Claude Code Only)

Restrict what the agent can do by passing `allowed_tools`. This is critical for safety when delegating work.

> **Important:** Tool scoping only works with Claude Code. OpenCode ignores `allowed_tools` — the agent will have access to all tools regardless of what you pass. Do not use OpenCode for safety-sensitive delegation where tool restriction is required.

> **Gotcha:** `allowed_tools=[]` (empty list) is falsy in Python, so no `--allowed-tools` flag is sent — the agent gets **full tool access**. To restrict tools, always pass a non-empty list. There is no way to disable all tools via this parameter.

```python
# Read-only analysis (Claude Code)
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

response = await shell.execute(
    cwd=project_path,
    prompt="Review this code for security issues",
    allowed_tools=["Read", "Glob", "Grep"],  # No write access
)

# Full write access for implementation
response = await shell.execute(
    cwd=project_path,
    prompt="Implement the fix you recommended",
    allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
)
```

## Error Handling

AgentShell raises exceptions for some errors, but CLI agent failures are reported as events, not exceptions.

```python
from pathlib import Path

# cwd validation - raises ValueError if directory doesn't exist
try:
    response = await shell.execute(cwd="/nonexistent", prompt="hello")
except ValueError as e:
    print(f"Bad directory: {e}")

# Unsupported agent type - raises ValueError at construction
try:
    shell = AgentShell(agent_type=AgentType.GEMINI_CLI)
except ValueError as e:
    print(f"No adapter: {e}")

# KeyboardInterrupt - AgentShell cancels the subprocess cleanly
try:
    response = await shell.execute(cwd=project_path, prompt="long task...")
except KeyboardInterrupt:
    print("Agent cancelled")
```

**CLI agent failures do not raise exceptions.** `execute()` returns an `AgentResponse` with whatever text was accumulated (which may be empty) and gives no indication of failure. When streaming, failures surface in two ways:

- `StreamEvent(type="error")` — CLI process errors or OpenCode agent errors
- `StreamEvent(type="result", content="error")` — Claude Code agent-level failures

Check for both when streaming:

```python
async for event in shell.stream(cwd=project_path, prompt="do something"):
    if event.type == "error":
        print(f"Agent error: {event.content}")
        break
    elif event.type == "result" and event.content == "error":
        print("Agent reported failure")
        break
    elif event.type == "text":
        print(event.content)
```

## Logging

```python
import logging

logging.getLogger("agent_shell").setLevel(logging.DEBUG)
logging.getLogger("agent_shell").addHandler(logging.StreamHandler())
```

`INFO` captures tool calls, session IDs, costs, errors. `DEBUG` adds raw JSON events.

## Quick Reference

| Want to... | Do this |
|------------|---------|
| Get a complete answer | `await shell.execute(cwd, prompt)` |
| Stream events live | `async for event in shell.stream(cwd, prompt)` |
| Continue a conversation | Pass `session_id=response.session_id` |
| Limit agent capabilities | Pass `allowed_tools=["Read", "Glob"]` |
| Track costs | Read `response.cost` or `event.cost` |
| Use a specific model | Pass `model="sonnet"` |
| Increase reasoning depth | Pass `effort="high"` |
| See agent thinking | Pass `include_thinking=True` |
| Cancel a running agent | `KeyboardInterrupt` (handled automatically) |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Passing a non-existent `cwd` | Validate the path exists before calling |
| Forgetting `await` on `execute()` | Both `execute()` and the async iterator from `stream()` require async context |
| Not scoping `allowed_tools` (Claude Code) | Always restrict tools to the minimum needed for the task |
| Assuming `allowed_tools` works with OpenCode | OpenCode ignores this parameter — use Claude Code for tool-restricted tasks |
| Expecting `execute()` to include thinking | `include_thinking` only affects `stream()` events; `execute()` drops thinking |
| Not checking for error events in `stream()` | Agent failures yield `StreamEvent(type="error")`, they don't raise exceptions |
| Ignoring `session_id` for multi-step work | Without it, each call starts a fresh conversation with no prior context |
| Using an unsupported `AgentType` | Only `CLAUDE_CODE` and `OPENCODE` have adapters currently |

## API Reference

For detailed model definitions, event types, and adapter protocol: see [api-reference.md](api-reference.md).
