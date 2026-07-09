---
name: invoking-cli-agents
description: Use when programmatically invoking a CLI coding agent (Claude Code, OpenCode, Copilot CLI, Codex, Pi) from Python, delegating a task to a sub-agent, orchestrating several agents, streaming an agent's output, resuming an agent session, restricting which tools an agent may use, or checking whether an agent/model is healthy. Keywords: AgentShell, headless agent, subprocess, allowed_tools, disallowed_tools, read-only agent, session_id, cost, output_tokens.
---

# Invoking CLI Agents with AgentShell

AgentShell is a Python library that runs CLI coding agents headlessly and returns
structured output. It hides agent-specific CLI differences behind a unified interface so
your code works regardless of which agent runs underneath.

It shells out to the agent's own CLI as a child subprocess (inheriting `cwd` and the parent
environment). The target CLI must be **installed and authenticated separately** — AgentShell
passes `model` strings through verbatim and does not manage credentials.

## When to Use

- You need to invoke Claude Code, OpenCode, Copilot CLI, Codex, or Pi from Python
- You want to delegate a coding task to a sub-agent and collect the result
- You need to orchestrate multi-step workflows across agents
- You want to stream agent output in real-time
- You need to restrict what tools a delegated agent can run
- You want to check whether an agent/model combination works before relying on it

## When NOT to Use

- You want to call the Anthropic API directly (use the SDK instead)
- The CLI agent is already running interactively and you just need its output

## Installation

```bash
uv add agent-shell-py
```

## Core Concepts

AgentShell has two invocation methods — `execute()` collects a complete response, `stream()`
yields events in real-time — plus helpers for health checks and MCP server management. All
are async.

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

print(response.response)                       # Full text output
print(f"Cost: ${response.cost:.4f}")           # 0.0 if the agent doesn't report cost
print(f"Output tokens: {response.output_tokens}")
print(f"Session: {response.session_id}")
```

### Stream: Real-Time Events

Use when you need progress feedback, want to display output incrementally, or need to react
to specific event types (tool use, thinking, errors).

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
    elif event.type == "error":
        print(f"[error] {event.content}")
    elif event.type == "result":
        print(f"Done ({event.content}). Cost: ${event.cost:.4f}, {event.output_tokens} tok")
```

### Session Resumption

Pass `session_id` from a previous response to continue the conversation. This enables
multi-turn workflows where each step builds on the last.

```python
analysis = await shell.execute(
    cwd="/path/to/project",
    prompt="Analyse this codebase and identify areas that need refactoring",
    allowed_tools=["Read", "Glob", "Grep"],
    model="sonnet",
)

refactor = await shell.execute(
    cwd="/path/to/project",
    prompt="Now refactor the top priority item you identified",
    allowed_tools=["Read", "Edit", "Bash"],
    model="sonnet",
    session_id=analysis.session_id,   # same conversation
)
```

## Parameters

Both `execute()` and `stream()` take the same parameters.

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `cwd` | `str` | required | Working directory (must exist, else `ValueError`) |
| `prompt` | `str` | required | Task or question for the agent |
| `allowed_tools` | `list[str] \| None` | `None` | Whitelist of tools (agent-native names). `None` = all tools. Honoured by Claude Code, Copilot CLI, Pi; ignored by OpenCode and Codex. **Only actually enforced when `auto_approve=False`** (see Tool Restriction). |
| `disallowed_tools` | `list[str] \| None` | `None` | Denylist using a canonical vocabulary (see Tool Restriction). Deny takes precedence over allow **and** over `auto_approve`, but covers only built-in tools. Enforcement varies per agent; unenforceable denies emit a `UserWarning`. |
| `model` | `str \| None` | `None` | Model alias or name, passed to the CLI verbatim (e.g. `"sonnet"`, `"opencode/big-pickle"`) |
| `effort` | `str \| None` | `None` | Reasoning effort: `"low"`, `"medium"`, `"high"`, etc. Claude Code, Copilot, Codex, Pi. **Ignored by OpenCode.** |
| `include_thinking` | `bool` | `False` | Yield `thinking` events in `stream()`. Claude Code, Copilot, Pi. **Dropped by `execute()`** (which keeps only text). |
| `auto_approve` | `bool` | `True` | Skip tool permission prompts. Mapped on every adapter (Pi *requires* a trust decision — the default avoids a hang). **On Claude Code the default `True` sends `--dangerously-skip-permissions`, which bypasses `allowed_tools`.** |
| `session_id` | `str \| None` | `None` | Resume a previous session |

> **`allowed_tools=[]` is a footgun.** An empty list is falsy in Python, so it is treated
> like `None` and the agent gets **full tool access**. To restrict tools, pass a non-empty
> `allowed_tools` *or* use `disallowed_tools`. There is no way to disable all tools via
> `allowed_tools`.

## Supported Agents

```python
from agent_shell.models.agent import AgentType

AgentType.CLAUDE_CODE   # Claude Code CLI
AgentType.OPENCODE      # OpenCode CLI
AgentType.COPILOT_CLI   # GitHub Copilot CLI
AgentType.CODEX         # OpenAI Codex CLI
AgentType.PI            # Pi coding agent
AgentType.GEMINI_CLI    # enum value only — NO adapter (raises ValueError at construction)
```

Capabilities differ by agent. `output_tokens` is populated on all of them; the rest varies:

| Agent | `allowed_tools` | `disallowed_tools` | `effort` | `cost` | `duration` | MCP mgmt |
|-------|:---:|---|:---:|---|:---:|:---:|
| Claude Code | ✅ | ✅ all canonical | ✅ | ✅ real | ✅ | ✅ |
| OpenCode | ❌ | ✅ all canonical | ❌ | ⚠️ often `0.0` | ❌ `0.0` | ✅ |
| Copilot CLI | ✅ | ⚠️ `bash`, `edit` only | ✅ | ❌ `0.0` | ✅ real | ✅ |
| Codex | ❌ | ⚠️ `web_search` only | ✅ | ❌ `0.0` | ❌ `0.0` | ✅ |
| Pi | ✅ | ⚠️ `bash`, `edit`, `read` | ✅ | ⚠️ paid providers only | ❌ `0.0` | ❌ raises |

A `✅` for `allowed_tools` means the flag is passed — but it only *enforces* with
`auto_approve=False`; `disallowed_tools` covers only built-in tools. See Tool Restriction.

## Tool Restriction (Safety)

Two independent controls with **different enforcement** — this trips people up, so read carefully.

- **`allowed_tools`** — a whitelist of the agent's *native* tool names (Claude Code's `"Read"`,
  `"Edit"`, `"Bash"`, …). Honoured only by Claude Code, Copilot CLI, and Pi. **It is only
  actually enforced when `auto_approve=False`.** With the default `auto_approve=True`, Claude
  Code runs under `--dangerously-skip-permissions`, which auto-approves *every* tool and
  silently defeats the whitelist (verified: an "allow Read/Glob/Grep" agent still wrote a file).
- **`disallowed_tools`** — a denylist in a small **canonical, cross-agent vocabulary**:
  `"bash"`, `"edit"`, `"read"`, `"web_search"`, `"web_fetch"`. Each adapter maps these to its
  native deny mechanism; **deny beats allow and beats `auto_approve`**, so it works under the
  default. `"edit"` covers the whole file-write family. Names outside the set pass through
  verbatim. But it **only covers the agent's built-in tools** — an MCP-provided or
  differently-named write/exec tool bypasses it (verified: with `["edit","bash"]` denied,
  Claude Code still wrote a file via an inherited MCP `create_text_file` tool).

**To actually restrict an agent:**

```python
# Enforced restriction on Claude Code / Copilot / Pi — whitelist + NO auto-approve.
# Everything not listed (Write, Edit, Bash, MCP tools) is denied.
response = await shell.execute(
    cwd=project_path,
    prompt="Review this code for security issues (the diff is below):\n" + diff_text,
    allowed_tools=["Read", "Glob", "Grep"],
    auto_approve=False,
)

# OpenCode / Codex ignore allowed_tools — use the enforced denylist instead.
# Keep auto_approve at its default True: on OpenCode, auto_approve=False makes `opencode run`
# auto-reject prompts and can silently abort the turn. (auto_approve=False is only for the
# whitelist path above, on Claude Code / Copilot / Pi.)
response = await shell.execute(
    cwd=project_path,
    prompt="Review this code for security issues",
    disallowed_tools=["edit", "bash"],   # enforced even under auto_approve
)
```

> **What is and isn't a guarantee.** `disallowed_tools=["edit"]` alone is *not* read-only — the
> model just writes via `bash` (`echo ... > file`). Denying both `edit` and `bash` removes the
> built-in write paths, but MCP-provided tools can still bypass it, and `allowed_tools` is inert
> under the default `auto_approve=True`. In-library tool scoping is defence-in-depth, not a
> sandbox. If a delegated agent must be *incapable* of writing (untrusted model, or reviewing
> hostile input that could prompt-inject it), enforce it **outside** the library: a read-only
> bind mount, a container, or a throwaway user. Always check for a `UserWarning` — an
> unenforceable deny (e.g. `["read"]` on Codex) is warned, not applied. To see the MCP bypass
> surface an agent would inherit, call `await shell.list_mcp_servers()` first.

## Error Handling

AgentShell raises exceptions for its own preconditions, but **CLI agent failures are reported
as events, not exceptions.**

```python
# cwd validation - raises ValueError if directory doesn't exist
# Unsupported agent (e.g. AgentType.GEMINI_CLI) - raises ValueError at construction
# KeyboardInterrupt - AgentShell cancels the subprocess cleanly, then re-raises
```

`execute()` returns an `AgentResponse` with whatever text accumulated (possibly empty) and
**no failure signal** — an empty `response` usually means the agent failed. To detect failures,
use `stream()` and treat success as a **positive** signal, the way the library's own
`health_check` does: a `result` event with `content == "ok"` arrived **and** no `error` event.

Failure shows up three ways, and the positive-signal check below catches all three: (1) an
`error` event; (2) a `result` event with `content == "error"` — how a bad model or usage limit
often surfaces, sometimes with *no* separate `error` event; (3) *no* `result` event at all —
a turn can truncate with no terminal event and no error (OpenCode in particular can drop the
tail and still exit 0), leaving a partial response that looks fine. Never infer success from
"got some text and no error event." Treat a missing `result` event as failure (and consider a
retry). Neither `execute()` nor `stream()` has a timeout, so wrap the call in `asyncio.wait_for`
to guard against a hang.

```python
saw_ok = False
error = None
async for event in shell.stream(cwd=project_path, prompt="do something"):
    if event.type == "error":
        error = event.content
    elif event.type == "result":
        saw_ok = event.content == "ok"       # content=="error" (or never arriving) => failure
    elif event.type == "text":
        print(event.content)

succeeded = saw_ok and error is None         # absent result => succeeded stays False
```

## Cost & Usage

- `output_tokens` — the portable "how much did it generate" signal; populated on the `result`
  event of every adapter. It reads `0` when the `result` event never arrives (a truncated turn),
  so it is not a standalone liveness check — pair it with the success check above.
- `cost` — real for Claude Code and paid Pi providers; frequently `0.0` for OpenCode,
  Copilot, and Codex (they don't report it). Don't treat `cost == 0` as "the call failed".
- `duration` — real only for Claude Code and Copilot CLI; `0.0` elsewhere.

## Other Capabilities

- **Health check** — `await shell.health_check(cwd, model=...)` returns a `HealthCheckResult`.
  It sends its *own* trivial no-tool prompt to confirm the agent/model completes a turn — it does
  **not** run your prompt, so use the stream-based check above when you care about a specific
  call's outcome.
- **MCP server management** — `add_mcp_server`, `remove_mcp_server`, `list_mcp_servers` manage
  the underlying CLI's MCP config (Pi raises `NotImplementedError`).

See [api-reference.md](api-reference.md) for their full signatures and the `MCPServerSpec` model.

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
| Whitelist tools (Claude/Copilot/Pi) | `allowed_tools=["Read", "Glob"]` |
| Deny tools on any agent (enforced) | `disallowed_tools=["edit", "bash"]` |
| Track usage | Read `response.output_tokens` (portable) or `response.cost` |
| Use a specific model | `model="sonnet"` |
| Increase reasoning depth | `effort="high"` (not OpenCode) |
| See agent thinking | `include_thinking=True` in `stream()` |
| Check an agent/model works | `await shell.health_check(cwd, model=...)` |
| Cancel a running agent | `KeyboardInterrupt` (handled automatically) |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Passing a non-existent `cwd` | Validate the path exists first (else `ValueError`) |
| Forgetting `await` | Both `execute()` and the `stream()` iterator are async |
| `allowed_tools=[]` to disable tools | Empty list is falsy → full access. Use a non-empty list or `disallowed_tools` |
| `allowed_tools` with the default `auto_approve=True` as a safety boundary | `--dangerously-skip-permissions` bypasses it. Set `auto_approve=False`, or use `disallowed_tools` |
| Relying on `allowed_tools` with OpenCode/Codex | They ignore it. Use `disallowed_tools` |
| Trusting a `prompt` instruction ("don't edit files") as a guarantee | Enforce it (`disallowed_tools`, or whitelist + `auto_approve=False`) |
| Assuming `disallowed_tools` sandboxes the agent | It covers only built-in tools; MCP/other-named tools bypass it. OS-sandbox for a hard guarantee |
| Treating `cost == 0` as failure | Many agents don't report cost; use `output_tokens` |
| Expecting `execute()` to expose thinking or detect failure | Use `stream()` for both |
| Ignoring `UserWarning` on a deny | An unenforceable deny is warned, not applied — the tool is NOT blocked |
| Ignoring `session_id` for multi-step work | Without it, each call starts fresh |
| Using `AgentType.GEMINI_CLI` | No adapter — raises `ValueError` at construction |

## API Reference

For model definitions, event types, per-agent behaviour, and the adapter protocol: see
[api-reference.md](api-reference.md).
