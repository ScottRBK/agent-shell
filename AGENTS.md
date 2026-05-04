# Agent Shell

A lightweight, async Python package that executes CLI coding agents headlessly and returns output through a unified interface. Each agent's CLI differences are hidden behind a common adapter protocol, so consuming code never changes regardless of which agent is running underneath.

## Architecture

```mermaid
classDiagram
    class AgentShell {
        -AgentAdapter _adapter
        +execute(cwd, prompt, ...) AgentResponse
        +stream(cwd, prompt, ...) AsyncIterator~StreamEvent~
        +add_mcp_server(spec) None
        +remove_mcp_server(name) None
        +list_mcp_servers() list~MCPServerSpec~
    }

    class AgentAdapter {
        <<Protocol>>
        +execute(cwd, prompt, ...) AgentResponse
        +stream(cwd, prompt, ...) AsyncIterator~StreamEvent~
        +cancel() None
        +add_mcp_server(spec) None
        +remove_mcp_server(name) None
        +list_mcp_servers() list~MCPServerSpec~
    }

    class ClaudeCodeAdapter {
        -list _active_processes
        +execute(cwd, prompt, ...) AgentResponse
        +stream(cwd, prompt, ...) AsyncIterator~StreamEvent~
        +cancel() None
        +add_mcp_server(spec) None
        +remove_mcp_server(name) None
        +list_mcp_servers() NotImplementedError
        -_parse_event(event, include_thinking) list~StreamEvent~
    }

    class MCPServerSpec {
        +str name
        +MCPServerType type
        +str command
        +list args
        +dict env
        +str url
        +dict headers
    }

    class MCPServerType {
        <<StrEnum>>
        STDIO
        HTTP
    }

    class AgentResponse {
        +str response
        +float cost
    }

    class StreamEvent {
        +str type
        +str content
        +float cost
        +float duration
    }

    class AgentType {
        <<StrEnum>>
        CLAUDE_CODE
        OPENCODE
        GEMINI_CLI
        COPILOT_CLI
        CODEX
    }

    AgentShell --> AgentAdapter : delegates to
    AgentShell --> AgentType : resolves via
    ClaudeCodeAdapter ..|> AgentAdapter : satisfies
    AgentShell ..> AgentResponse : returns
    AgentShell ..> StreamEvent : yields
    AgentShell ..> MCPServerSpec : accepts/returns
    MCPServerSpec --> MCPServerType : typed by
    ClaudeCodeAdapter ..> StreamEvent : parses NDJSON into
```

The adapter pattern uses Python's `Protocol` (structural typing) rather than ABC, so adapters satisfy the contract implicitly without inheritance. Each adapter manages its own subprocess lifecycle, translating agent-specific CLI flags and NDJSON output into the shared `StreamEvent`/`AgentResponse` models.

## Supported Agents

- [x] Claude Code
- [x] OpenCode
- [x] Copilot CLI
- [ ] Gemini CLI
- [ ] Codex

## MCP Server Configuration

`AgentShell` exposes a unified API for registering MCP servers across all supported agents:

```python
from agent_shell import AgentShell
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

Adds are idempotent (overwrite existing entries with the same name). Removes warn rather than raise when the named server is not found. `list_mcp_servers()` is not yet implemented for Claude Code.

## Test Philosophy

Tests validate real functionality, not code coverage metrics. Three tiers, each with a distinct purpose:

| Tier | Scope | Runs in CI | Real CLI calls |
|------|-------|-----------|----------------|
| **Unit** | Isolated functions (`_parse_event`, adapter resolution, input validation) | Yes | No |
| **Integration** | Full flow through `AgentShell` -> `Adapter` -> parser with mocked subprocess | Yes | No |
| **E2E** | Real CLI agent calls, real API costs | No (local only) | Yes |

Integration tests mirror the E2E tests exactly but substitute a mocked subprocess emitting captured NDJSON fixtures. This means CI validates the entire class interaction chain without credentials or API spend. E2E tests exist as a local smoke test to confirm the real agents still behave as expected.

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
