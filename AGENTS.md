# Agent Shell

A lightweight, async Python package that executes CLI coding agents headlessly and returns output through a unified interface. Each agent's CLI differences are hidden behind a common adapter protocol, so consuming code never changes regardless of which agent is running underneath.

## Architecture

Before any planning, discussion of solution or code change work begins, you must read the
[architecture class diagram](docs/architecture/class_diagram.md). It documents the interfaces,
execution hosts, isolation policies, interactive sessions, and API behaviour.

## Supported Agents

- [x] Claude Code
- [x] OpenCode
- [x] Copilot CLI
- [x] Codex
- [x] Pi
- [x] Cursor
- [x] Grok

## MCP Server Configuration

`AgentShell` exposes a unified API for registering MCP servers across all supported agents:

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

await shell.add_mcp_server(MCPServerSpec(
    name="forgetful",
    type=MCPServerType.STDIO,
    command="uvx",
    args=["forgetful-ai"],
    env={"FORGETFUL_API_KEY": "..."},
))
```

All adapters write to user-scope configuration:

| Agent | Mechanism | Location |
|-------|-----------|----------|
| Claude Code | `claude mcp add --scope user` subprocess | `~/.claude.json` (managed by CLI) |
| OpenCode | direct JSON file write | `~/.config/opencode/opencode.json` |
| Copilot CLI | direct JSON file write | `~/.copilot/mcp-config.json` |
| Codex | `codex mcp add` subprocess | Codex config |
| Cursor | direct JSON file write | `~/.cursor/mcp.json` |
| Grok | `grok mcp add --scope user` subprocess | `~/.grok/config.toml` (managed by CLI) |

Adds are idempotent (update existing entries with the same name). Cursor preserves native fields
that `MCPServerSpec` cannot represent when an update keeps the same transport, and writes its file
atomically with user-only permissions. Removes warn rather than raise when the named server is not
found. Claude Code listing reads the user-scope `mcpServers`
entries from `~/.claude.json` directly, avoiding the health checks and human-readable output of
`claude mcp list`. Cursor manages user-scope MCP entries directly in `~/.cursor/mcp.json` because
its `mcp` subcommands have no add/remove commands. Grok listing reads user-scope `mcp_servers`
entries from `~/.grok/config.toml` directly for the same reason. Pi's MCP add/remove/list methods
raise `NotImplementedError`.

## Package Management

See [package management](docs/development/package_management.md) for the API, Pi behaviour,
scope, and validation details.

## Test Philosophy

Tests validate real functionality, not code coverage metrics. Three tiers, each with a distinct purpose:

| Tier | Scope | Runs in CI | Real CLI calls |
|------|-------|-----------|----------------|
| **Unit** | Isolated functions (`_parse_event`, adapter resolution, input validation) | Yes | No |
| **Integration** | Full flow through `AgentShell` -> `Adapter` -> parser with mocked subprocess | Yes | No |
| **E2E** | Real CLI calls; usually real API costs | No (local only) | Yes |

The model-discovery E2E test is the exception: it calls all seven real CLIs but only reads
metadata, so it sends no inference request and incurs no model-token cost.

Integration tests mirror the E2E tests but substitute mocked subprocesses emitting captured CLI
output fixtures. This lets CI validate the full class interaction chain without credentials or API
spend. E2E tests remain local smoke tests for the real agents.

All tests follow the **AAA pattern** (Arrange, Act, Assert).

```bash
# CI suite (unit + integration)
uv run pytest tests/unit tests/integration -v

# Full suite including E2E (requires agent CLI + credentials)
uv run pytest -v
```

## CI/CD

- **CI**: Runs unit + integration tests on every push and PR
- **Build**: Triggers on `v*` tags, runs tests then builds sdist + wheel artifacts for release
