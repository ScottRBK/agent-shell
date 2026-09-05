# Agent Shell
Agent Shell is a light weight abstraction for executing a cli coding agent headlessly
and returning the output that can be used programatically as a unified contract

## Features

- **One unified contract** — the same `execute`, `stream`, `health_check`, and
  `list_models` API across every agent; swap the backend without changing consuming code.
- **Seven CLI agents** — Claude Code, OpenCode, Copilot CLI, Codex, Pi, Cursor, and Grok
  behind a common adapter protocol.
- **Execute or stream** — get one `AgentResponse` (raises `AgentExecutionError` on a failed run),
  or async-iterate normalized `StreamEvent`s with optional thinking/reasoning.
- **Composable execution policy** — preserve native execution by default, or opt into Linux PID
  namespace isolation without changing an agent adapter.
- **Session resumption** — continue any conversation by passing back its `session_id`.
- **Normalized cost & tokens** — consistent `cost` and `output_tokens` (reasoning included)
  regardless of how each CLI reports them.
- **Model discovery** — retrieve the exact account/workspace-aware model strings accepted by
  each CLI, without inference calls, SDK dependencies, or static catalogs.
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

## Agent skills

![Skill_Banner](docs/assets/skill_banner.png)

The repository includes reusable skills that teach coding agents how to use AgentShell:

- `invoking-cli-agents` — invoke, stream, resume, and restrict CLI agents.
- `delegating-code-review` — delegate an independent code review through AgentShell.

Install them interactively with the Vercel Skills CLI:

```bash
npx skills add ScottRBK/agent-shell
```

Or install both skills globally for every coding agent supported by AgentShell:

```bash
npx skills add ScottRBK/agent-shell --global \
  --skill '*' \
  --agent claude-code opencode github-copilot codex pi cursor grok \
  --yes
```

Install only the core AgentShell skill with:

```bash
npx skills add ScottRBK/agent-shell --skill invoking-cli-agents
```

The skills provide agent instructions. Install `agent-shell-py` and the chosen coding-agent CLIs
separately.

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

See [more examples](docs/examples.md) for isolation and execution hosts, failure handling,
streaming, model discovery, health checks, tool restrictions, MCP servers, and logging.
