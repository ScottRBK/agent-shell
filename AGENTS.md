# Agent Shell

A lightweight, async Python package that executes CLI coding agents headlessly and returns output
through a unified interface. Each agent's CLI differences are hidden behind a common adapter
protocol, so consuming code never changes regardless of which agent is running underneath.

## Architecture

Before any planning, discussion of solution or code change work begins, you must read the
[architecture class diagram](docs/architecture/class_diagram.md). It documents the interfaces,
execution hosts, isolation policies, interactive sessions, and API behaviour.

Keep agent-specific CLI flags, raw output parsing, configuration formats, and workarounds in
that agent's adapter or agent-specific helpers. Shared code must use protocols and normalised
models, not agent-name branches, concrete-adapter checks, private adapter state, or copied
agent-specific logic. Host-specific terminal/process details belong in execution hosts.
Concrete agent-adapter imports are permitted only in `shell.py` for construction/selection;
shared helpers under `adapters/` are not exempt. Import Linter contracts in `pyproject.toml`
enforce the [dependency boundaries](docs/architecture/dependency_boundaries.md), including
indirect imports. New inner code uses `ports/`, not implementation-bearing compatibility
facades. New top-level modules and adapter helpers must be classified in the contracts.
Review must still catch copied agent-specific logic and dynamic imports.
If a shared contract cannot express a feature, discuss the boundary change before adding a
workaround or weakening the test.

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

Tests validate real functionality, not code coverage metrics. Three tiers, each with a distinct
purpose:

| Tier | Scope | Runs in CI | Real CLI calls |
|------|-------|-----------|----------------|
| **Unit** | Isolated parsing, adapter resolution, input validation | Yes | No |
| **Integration** | Shell -> adapter -> parser, with mocked agent subprocess | Yes | No |
| **E2E** | Real CLI calls; usually real API costs | No (local only) | Yes |

The model-discovery E2E test is the exception: it calls all seven real CLIs but only reads
metadata, so it sends no inference request and incurs no model-token cost.

Integration tests mirror the E2E tests but substitute mocked subprocesses emitting captured CLI
output fixtures. This lets CI validate the full class interaction chain without credentials or API
spend. E2E tests remain local smoke tests for the real agents.

All tests follow the **AAA pattern** (Arrange, Act, Assert).

```bash
# Architecture contracts (development dependency only)
uv run --locked lint-imports --no-cache

# CI suite (unit + integration, including deliberate boundary violations in temp copies)
uv run --locked pytest tests/unit tests/integration -v

# Full suite including E2E (requires agent CLI + credentials)
uv run pytest -v
```

## CI/CD

- **CI**: Runs architecture contracts and unit + integration tests on every push and PR
- **Build**: Triggers on `v*` tags, runs contracts/tests then builds release artifacts
