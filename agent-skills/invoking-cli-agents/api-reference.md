# AgentShell API Reference

- [Models](#models) — `AgentType`, `AgentResponse`, `StreamEvent`, `MCPServerSpec`, `HealthCheckResult`
- [StreamEvent types](#event-types)
- [AgentShell class](#agentshell-class) — `execute`, `stream`, `health_check`, MCP management
- [AgentAdapter protocol](#agentadapter-protocol)
- [Agent-specific notes](#agent-specific-notes)

## Models

### AgentType

```python
from agent_shell.models.agent import AgentType

class AgentType(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    GEMINI_CLI = "gemini_cli"   # enum only — NO adapter (raises ValueError)
    COPILOT_CLI = "copilot_cli"
    CODEX = "codex"
    PI = "pi"
```

### AgentResponse

Returned by `execute()`.

```python
@dataclass
class AgentResponse:
    response: str            # Full text output from the agent
    cost: float              # Total cost in USD (0.0 if the agent doesn't report it)
    session_id: str | None = None  # Use to resume this conversation
    duration: float = 0.0    # Wall-clock seconds (0.0 unless the agent reports it)
    output_tokens: int = 0   # Generated tokens (reasoning-inclusive; populated on all agents)
```

### StreamEvent

Yielded by `stream()`.

```python
@dataclass
class StreamEvent:
    type: str                # Event type (see below)
    content: str             # Event payload
    cost: float = 0.0        # Cumulative cost in USD (on "result" events)
    duration: float = 0.0    # Elapsed seconds (on "result" events, where supported)
    session_id: str | None = None  # On session-start and "result" events
    output_tokens: int = 0   # Cumulative generated tokens (on "result" events)
```

### MCPServerSpec

Used by the MCP-management methods. `__post_init__` validates the transport (STDIO requires
`command` and forbids `url`/`headers`; HTTP requires `url` and forbids `command`/`args`/`env`).

```python
from agent_shell.models.agent import MCPServerSpec, MCPServerType

class MCPServerType(StrEnum):
    STDIO = "stdio"
    HTTP = "http"

@dataclass
class MCPServerSpec:
    name: str
    type: MCPServerType
    command: str | None = None          # STDIO
    args: list[str] = []                # STDIO
    env: dict[str, str] = {}            # STDIO
    url: str | None = None              # HTTP
    headers: dict[str, str] = {}        # HTTP
```

### HealthCheckResult

```python
@dataclass
class HealthCheckResult:
    healthy: bool
    exception: str | None = None   # failure detail when healthy is False
```

## Event Types

Canonical event types emitted by `stream()`:

| Type | When | Content | Other fields |
|------|------|---------|--------------|
| `system` | Session starts | `""` | `session_id` is set |
| `text` | Agent produces output | Response text (may arrive in chunks) | |
| `thinking` | Agent reasons (requires `include_thinking=True`; Claude Code / Copilot / Pi) | Chain-of-thought text | |
| `tool_use` | Agent invokes a tool | Tool name (Codex: the command string) | |
| `result` | Agent finishes | `"ok"` on success, `"error"` on agent-level failure | `cost`, `duration`, `output_tokens`, `session_id` populated where supported |
| `error` | Agent or CLI process fails | Error message / stderr tail (last 500 chars) | |

> Codex emits the session-start event as `type="session"` (not `"system"`). If you branch on
> the session event across agents, match both.

## AgentShell Class

```python
from agent_shell.shell import AgentShell

class AgentShell:
    def __init__(self, agent_type: AgentType): ...
    # raises ValueError for an AgentType with no adapter (e.g. GEMINI_CLI)

    async def execute(
        self,
        cwd: str,                               # Working directory (must exist)
        prompt: str,                            # Task for the agent
        allowed_tools: list[str] | None = None, # Whitelist (None = all); not all agents honour it
        model: str | None = None,               # Model alias or full ID
        effort: str | None = None,              # "low" | "medium" | "high" | ...
        include_thinking: bool = False,         # Include chain-of-thought (stream only)
        auto_approve: bool = True,              # Skip tool permission prompts
        session_id: str | None = None,          # Resume previous session
        disallowed_tools: list[str] | None = None,  # Canonical denylist (enforced; deny > allow)
    ) -> AgentResponse: ...

    def stream(self, ...) -> AsyncIterator[StreamEvent]: ...   # same parameters as execute()

    async def health_check(
        self, cwd: str, model: str | None = None, timeout: float = 60.0,
    ) -> HealthCheckResult: ...
    # Sends a trivial no-tool prompt; healthy iff a result=="ok" event arrives and no error.

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None: ...
    async def remove_mcp_server(self, mcp_server_name: str) -> None: ...
    async def list_mcp_servers(self) -> list[MCPServerSpec]: ...
```

### disallowed_tools canonical vocabulary

`disallowed_tools` accepts these canonical names; each adapter maps them to native deny
mechanisms. Names outside this set pass through verbatim (deny a specifically-named tool).
`edit` always covers the whole file-write family. Deny takes precedence over `allowed_tools`
and over `auto_approve`. An unenforceable deny emits a `UserWarning` — it is NOT applied.

```
CANONICAL_TOOLS = {"bash", "edit", "read", "web_search", "web_fetch"}
```

## AgentAdapter Protocol

To add a new CLI agent, implement this protocol (structural typing — no inheritance required):

```python
from typing import Protocol, AsyncIterator
from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, HealthCheckResult

class AgentAdapter(Protocol):
    async def execute(self, cwd, prompt, allowed_tools=None, model=None, effort=None,
                      include_thinking=False, auto_approve=True, session_id=None,
                      disallowed_tools=None) -> AgentResponse: ...

    def stream(self, ...) -> AsyncIterator[StreamEvent]: ...   # same signature as execute()

    async def cancel(self) -> None: ...

    async def health_check(self, cwd, model=None, timeout=60.0) -> HealthCheckResult: ...
    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None: ...
    async def remove_mcp_server(self, mcp_server_name: str) -> None: ...
    async def list_mcp_servers(self) -> list[MCPServerSpec]: ...
```

## Agent-Specific Notes

### Claude Code
- `model` accepts aliases (`"sonnet"`, `"opus"`, `"haiku"`) or full IDs.
- `allowed_tools` → `--allowed-tools`; native names `"Read"`, `"Edit"`, `"Write"`, `"Bash"`, `"Glob"`, `"Grep"`, etc.
- `disallowed_tools` → `--disallowed-tools` (all canonical names supported; `edit` → `Edit,Write,NotebookEdit`). Takes precedence over `--allowed-tools` and `--dangerously-skip-permissions`.
- `effort` → `--effort`; `auto_approve` → `--dangerously-skip-permissions`.
- `include_thinking` adds no CLI flag — it only filters thinking already present in the stream.
- `cost` and `duration` are real. MCP managed via the `claude mcp` CLI.

### OpenCode
- `model` uses provider-prefixed names (`"anthropic/claude-sonnet-4-5"`, `"github-copilot/gpt-5.4"`, `"opencode/big-pickle"`).
- `allowed_tools`, `effort`, `include_thinking` are **ignored** (no thinking events emitted).
- `disallowed_tools` **is enforced** via a per-subprocess `OPENCODE_PERMISSION` env var (all canonical names supported). This holds even under `--dangerously-skip-permissions` — a deny rule short-circuits before the auto-approved permission prompt.
- `auto_approve` → `--dangerously-skip-permissions` (without it, `opencode run` auto-*rejects* prompts non-interactively and can silently abort).
- `cost` is frequently `0.0`; `duration` is always `0.0`. MCP managed via the config file.

### Copilot CLI
- `allowed_tools` → repeated `--allow-tool`; `disallowed_tools` → `--deny-tool` but only `bash`→`shell` and `edit`→`write` are mapped (`read`/`web_search`/`web_fetch` warn as unenforceable; pass a verbatim native name if you know your build's).
- `effort` → `--effort`; `auto_approve` → `--allow-all-tools`; `include_thinking` → `--enable-reasoning-summaries`.
- `duration` is real; `cost` is always `0.0`. MCP managed via the config file.

### Codex
- `allowed_tools` is **ignored** (warns). The only `disallowed_tools` name it can enforce is `web_search` (a config override); anything else warns. `web_search` deny is silently ignored at `effort="minimal"` (upstream bug) and warns then too.
- `effort` → `-c model_reasoning_effort=...`; `auto_approve` → `--dangerously-bypass-approvals-and-sandbox`; `include_thinking` has no effect (warns).
- `cost` and `duration` are `0.0`. Session-start event is `type="session"`. MCP via the `codex mcp` CLI.

### Pi
- `allowed_tools` → `--tools`; `disallowed_tools` → `--exclude-tools` (`bash`, `edit`→`edit,write`, `read`; `web_search`/`web_fetch` warn — Pi has no web tool).
- `effort` → `--thinking` (levels: off/minimal/low/medium/high/xhigh); `auto_approve` → `--approve` / `--no-approve` (one is always sent, else `pi -p` hangs on a trust prompt).
- `cost` is real for paid providers (`0.0` on local); `duration` is `0.0`.
- MCP-management methods raise `NotImplementedError`.
