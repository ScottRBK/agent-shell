# Agent Shell
Agent Shell is a light weight abstraction for executing a cli coding agent headlessly
and returning the output that can be used programatically as a unified contract

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

## Copilot CLI

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Can you tell me about this project?",
    model="gpt-4o",
)

print(response.response)
print(f"Session: {response.session_id}")

# Resume the conversation using the session_id
follow_up = await shell.execute(
    cwd="/path/to/project",
    prompt="Now refactor the auth module based on your findings",
    session_id=response.session_id,
)
```

> **Note:** Copilot CLI doesn't expose pricing data. The `cost` field on `AgentResponse` will always be `0.0`. The `duration` field is populated from `usage.totalApiDurationMs`.

## Supported CLI Agents:

- [x] Claude Code
- [x] OpenCode
- [x] Copilot CLI
- [ ] Gemini CLI
- [ ] Codex




