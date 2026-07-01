# Agent Shell
Agent Shell is a light weight abstraction for executing a cli coding agent headlessly
and returning the output that can be used programatically as a unified contract

## Features

- **One unified contract** — the same `execute` / `stream` / `health_check` API across every
  agent; swap the backend without changing a line of consuming code.
- **Five CLI agents** — Claude Code, OpenCode, Copilot CLI, Codex, and Pi behind a common
  adapter protocol.
- **Execute or stream** — get one `AgentResponse`, or async-iterate normalized `StreamEvent`s
  with optional thinking/reasoning.
- **Session resumption** — continue any conversation by passing back its `session_id`.
- **Normalized cost & tokens** — consistent `cost` and `output_tokens` (reasoning included)
  regardless of how each CLI reports them.
- **Health checks** — confirm an agent + model combination actually works before you rely on
  it, read from the event stream rather than unreliable exit codes.
- **Portable tool control** — one canonical allow/deny vocabulary
  (`bash, edit, read, web_search, web_fetch`) translated to each CLI's own tool names.
- **Unified MCP management** — register, remove, and list MCP servers across agents through a
  single API.
- **Async & dependency-free** — pure `asyncio`, zero runtime dependencies, Python 3.12+.

## Installation

```bash
uv add agent-shell-py
```

or with pip:

```bash
pip install agent-shell-py
```

## Examples

### Execute

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Can you tell me about this project?",
    allowed_tools=["Read", "Glob", "Grep"],
    model="sonnet",
)

print(response.response)
print(f"Cost: ${response.cost:.4f}")
print(f"Output tokens: {response.output_tokens}")  # billed output, reasoning included
print(f"Session: {response.session_id}")

# Resume the conversation using the session_id
follow_up = await shell.execute(
    cwd="/path/to/project",
    prompt="Now refactor the auth module based on your findings",
    allowed_tools=["Read", "Edit", "Bash"],
    model="sonnet",
    session_id=response.session_id,
)
```

> `output_tokens` is a cost measure: the billed output-token count, which **includes reasoning
> tokens** (they are billed at the output rate). It is reported consistently across all adapters.

### Stream

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

async for event in shell.stream(
    cwd="/path/to/project",
    prompt="Refactor the auth module",
    allowed_tools=["Read", "Edit", "Bash"],
    model="sonnet",
    effort="high",
    include_thinking=True,
):
    if event.type == "system":
        print(f"Session: {event.session_id}")
    else:
        print(f"[{event.type}] {event.content}")
```

### Health check

Verify an agent + model combination actually works before relying on it. It sends a
trivial prompt and reports whether a real response came back — catching bad model names,
missing credentials, and billing/quota failures. Exit codes alone are unreliable (some
CLIs exit 0 on failure), so the verdict is read from the normalized event stream.

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

result = await shell.health_check(cwd="/path/to/project", model="haiku")
if not result.healthy:
    print(f"unavailable: {result.exception}")
```

### Restricting tools (`disallowed_tools`)

Pass a deny-list of tools that the agent must not use. Use the canonical vocabulary
`{bash, edit, read, web_search, web_fetch}` and Agent Shell translates it to each CLI's
own tool names — callers don't need to know the per-harness vocabulary:

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Audit this code but don't run anything or touch the network",
    disallowed_tools=["bash", "web_search", "web_fetch"],
)
```

- `edit` covers write/edit/notebook-edit (it fans out on harnesses that split them).
- Any name outside the canonical set passes through **verbatim** (e.g. an MCP tool
  `mcp__server__tool`, or a harness-specific name like `Write`, or Copilot's `view`).
- Deny takes precedence over auto-approve on every backend that supports it.
- Where a backend cannot enforce a deny, the adapter emits a `UserWarning` listing the
  ignored tools rather than failing silently. Coverage varies: Claude and OpenCode enforce
  all five canonical names; Copilot enforces only `bash`/`edit` canonically (use a verbatim
  name for its other tools); Codex can only deny `web_search`.
- Denying `edit` or `read` is **best-effort**: a model can still modify or read files through
  the shell, so also deny `bash` when you need a hard file boundary.

### OpenCode

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.OPENCODE)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Can you tell me about this project?",
    model="anthropic/claude-sonnet-4-5",
)

print(response.response)
print(f"Session: {response.session_id}")

# Resume the conversation using the session_id
follow_up = await shell.execute(
    cwd="/path/to/project",
    prompt="Now refactor the auth module based on your findings",
    model="anthropic/claude-sonnet-4-5",
    session_id=response.session_id,
)
```

> **Note:** OpenCode's `run` mode auto-approves all tools. The `allowed_tools` and `effort` parameters are configured via `opencode.json`, not CLI flags.

## MCP Servers

Register MCP servers for any supported agent through a unified API. All adapters use user-scope configuration so registrations persist across the agent's `execute`/`stream` calls.

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

# Register a stdio MCP server (e.g. forgetful) before running an eval
await shell.add_mcp_server(MCPServerSpec(
    name="forgetful",
    type=MCPServerType.STDIO,
    command="uvx",
    args=["forgetful-ai"],
    env={"FORGETFUL_API_KEY": "..."},
))

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Recall any prior decisions about the auth module",
)

# Optional cleanup
await shell.remove_mcp_server("forgetful")
```

For HTTP transport, pass `url` and `headers` instead of `command`/`args`/`env`:

```python
await shell.add_mcp_server(MCPServerSpec(
    name="remote",
    type=MCPServerType.HTTP,
    url="https://example.com/mcp",
    headers={"Authorization": "Bearer ..."},
))
```

`add_mcp_server` overwrites an existing server with the same name. `remove_mcp_server` warns rather than raises when the named server is not found. `list_mcp_servers()` works for OpenCode and Copilot CLI; for Claude Code it currently raises `NotImplementedError`.

## Logging

Agent Shell uses Python's standard `logging` module. Configure the `agent_shell` logger to capture tool calls, session IDs, costs, and errors:

```python
import logging

logging.getLogger("agent_shell").setLevel(logging.INFO)
logging.getLogger("agent_shell").addHandler(logging.StreamHandler())
```

Set to `DEBUG` for raw JSON events and full command arguments.

