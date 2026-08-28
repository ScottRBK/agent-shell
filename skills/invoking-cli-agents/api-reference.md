# AgentShell API Reference

- [Models](#models) — `AgentType`, `AgentResponse`, `AgentExecutionError`, `StreamEvent`,
  `MCPServerSpec`, `HealthCheckResult`
- [StreamEvent types](#event-types)
- [AgentShell class](#agentshell-class) — invocation, model discovery, health, MCP management
- [Execution hosts and isolation](#execution-hosts-and-isolation)
- [AgentAdapter protocol](#agentadapter-protocol)
- [Agent-specific notes](#agent-specific-notes)

## Models

### AgentType

```python
from agent_shell.models.agent import AgentType

class AgentType(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    COPILOT_CLI = "copilot_cli"
    CODEX = "codex"
    PI = "pi"
    CURSOR = "cursor"
    GROK = "grok"
```

### AgentResponse

Returned by `execute()` on success.

```python
@dataclass
class AgentResponse:
    response: str            # Full text output from the agent
    cost: float              # Total cost in USD (0.0 if the agent doesn't report it)
    session_id: str | None = None  # Use to resume this conversation
    duration: float = 0.0    # Wall-clock seconds (0.0 unless the agent reports it)
    output_tokens: int = 0   # Generated tokens (reasoning-inclusive; populated on all agents)
```

### AgentExecutionError

Raised by `execute()` — never returned — when the run failed: an `error` event was emitted, the
terminal `result` event had `content == "error"`, or no terminal `result` event arrived at all
(a killed, truncated, or aborted run). `str(e)` is the bare reason (e.g. `"500 model
name=qwen3.6-27b-8Q failed to load"`). The constructor arguments double as attributes, carrying
whatever partial data the run produced before failing.

```python
from agent_shell.models.agent import AgentExecutionError

class AgentExecutionError(Exception):
    def __init__(
        self,
        reason: str,              # str(e) == reason; also e.reason
        response: str = "",       # text produced before the failure, if any
        cost: float = 0.0,
        session_id: str | None = None,
        duration: float = 0.0,
        output_tokens: int = 0,
        returncode: int | None = None,  # raw process status; negative means signal
        signal: int | None = None,      # positive signal number for signal termination
    ): ...
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
    error: str | None = None # Why a failing "result" failed, when recoverable (Pi)
    returncode: int | None = None  # Set on process-level "error" events
    signal: int | None = None      # Positive signal number when returncode is negative
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
| `thinking` | Agent reasons (requires `include_thinking=True`; Claude Code / Copilot / Pi / Cursor) | Chain-of-thought text | |
| `tool_use` | Agent invokes a tool | Tool name (Codex, and Cursor shell calls: the command) | |
| `result` | Agent finishes | `"ok"` or `"error"` | see the note below |
| `error` | Agent or CLI process fails | Error message, or stderr head+tail (500 chars each) | |

> A `result` event carries `cost`, `duration`, `output_tokens` and `session_id` on the agents
> that report them. On a failing result, `error` holds the reason when the adapter recovered a
> structured one (Pi); it is `None` otherwise. A process-level `error` also carries
> `returncode`; if a signal terminated it, `returncode` is negative and `signal` is positive.

> Codex emits the session-start event as `type="session"` (not `"system"`). If you branch on
> the session event across agents, match both.

> Long stderr is **not** tail-only. `format_stderr` keeps the first 500 *and* the last 500
> characters, joined by a `... [truncated] ...` marker, so a reason stated up front
> (cursor-agent's `Cannot use this model: <name>`) survives alongside a trailing stack trace.
> A stderr of 1000 characters or fewer is passed through whole, with no marker.

## AgentShell Class

```python
from agent_shell.shell import AgentShell

class AgentShell:
    def __init__(
        self,
        agent_type: AgentType,
        execution_host: ExecutionHost | None = None,      # default NativeExecutionHost()
        isolation_policy: IsolationPolicy | None = None,  # default NoIsolation()
    ): ...
    # raises ValueError for an AgentType with no registered adapter

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
    # Raises AgentExecutionError if the run failed; see the Models section above.

    def stream(self, ...) -> AsyncIterator[StreamEvent]: ...   # same parameters as execute()

    async def health_check(
        self, cwd: str, model: str | None = None, timeout: float = 60.0,
    ) -> HealthCheckResult: ...
    # Sends a trivial no-tool prompt; healthy iff the LAST result event is "ok" and no error.

    async def list_models(
        self, cwd: str, timeout: float = 30.0,
    ) -> list[str]: ...
    # Returns exact selectors accepted by execute(model=...) and stream(model=...).

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None: ...
    async def remove_mcp_server(self, mcp_server_name: str) -> None: ...
    async def list_mcp_servers(self) -> list[MCPServerSpec]: ...
```

## Execution Hosts and Isolation

```python
from agent_shell.execution import (
    ExecutionHost,
    IsolationPolicy,
    IsolationUnavailableError,
    LinuxPidNamespaceIsolation,
    NativeExecutionHost,
    NativeRunHandle,
    NoIsolation,
    PreparedLaunch,
    RunHandle,
)
```

`ExecutionHost` creates a `RunHandle` for one command. The handle exposes `pid`, `stdin`,
`stdout`, `stderr`, `returncode`, `wait()`, `communicate()`, `cancel()`, and `release()`.
`NativeExecutionHost` is currently the only concrete host. Agent adapters use handles internally;
existing `execute()` and `stream()` callers do not need to manage them.

`NoIsolation` preserves historical native execution. `LinuxPidNamespaceIsolation` uses rootless
user + PID namespaces, with a tiny PID 1 reaper and the CLI at PID 2 or later. It protects
AgentShell's ancestors from direct same-user signals sent inside the child namespace. It is not a
filesystem, credential, network, tool, resource, or general security sandbox. Background
descendants terminate when the namespace ends.

The Linux policy requires `unshare` and supporting kernel configuration. An explicit request that
cannot be satisfied raises `IsolationUnavailableError` before launching the CLI and never falls
back to `NoIsolation`. The host/policy selection applies to `execute()`, `stream()`, and
`health_check()`; model discovery and MCP configuration remain local management operations.

### Model discovery semantics

`list_models()` reads the selected CLI's account/workspace-aware catalog without sending an
inference prompt. It preserves harness order and aliases such as `auto` and `default`. Pi
selectors are provider-qualified (`provider/model`) because model IDs can repeat across
providers.

A returned string is advertised as selectable, not guaranteed runnable at that moment. Use
`health_check(model=...)` to validate credentials, entitlement, quota, and provider health.
A genuine empty catalog returns `[]`; timeout, authentication, CLI, and malformed-output
failures are raised. AgentShell does not cache or substitute a static catalog.

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
    # Raises AgentExecutionError if the run failed.

    def stream(self, ...) -> AsyncIterator[StreamEvent]: ...   # same signature as execute()

    async def cancel(self) -> None: ...

    async def health_check(self, cwd, model=None, timeout=60.0) -> HealthCheckResult: ...
    async def list_models(self, cwd, timeout=30.0) -> list[str]: ...
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

### Cursor
- `model` → `--model` (e.g. `"sonnet-4-thinking"`, `"gpt-5"`); parameterised models take bracket
  overrides, e.g. `"claude-opus-4-8[context=1m,effort=high]"`. A Free plan exposes only `auto`.
- `allowed_tools` **and** `disallowed_tools` are both **ignored** (both warn). Cursor has no
  per-call tool policy of any kind — it lives in `.cursor/cli.json` — so *nothing* in this library
  can scope a Cursor agent; restrict it outside (read-only mount, container). `disallowed_tools`
  warns on every call; `allowed_tools` and `effort` warn once per adapter instance.
- `effort` is **ignored** (warns): Cursor has no effort flag, only the model bracket-override
  above, which the adapter does not inject. `include_thinking` **is** honoured — Cursor streams
  reasoning as `thinking` deltas.
- `auto_approve` → `--force`; without it tools auto-*reject* but the run still completes (exit 0).
  `--trust` is always sent regardless: untrusted, `cursor-agent` exits 1 with zero stdout and a
  plain-text "Workspace Trust Required" on stderr.
- Session resume is `--resume=<id>` (the `=` form binds the id, whose CLI arg is optional). The
  resumed run reports the SAME id — but an unknown id is **accepted**, creating a session under
  it rather than failing (like Pi; unlike Claude Code, OpenCode, Copilot and Codex, which all
  reject one). A matching id is therefore not proof a prior transcript was continued.
- `duration` and `output_tokens` are real (`usage.outputTokens`); `cost` is always `0.0` — Cursor
  reports no cost.
- MCP add/remove/list are supported by directly managing user-scope `~/.cursor/mcp.json`, because
  `cursor-agent mcp` has no add/remove subcommands and its list output lacks full configuration.
  Writes are atomic and user-only. Same-transport updates preserve Cursor-native fields that
  `MCPServerSpec` cannot represent.

### Grok
- Headless: `grok -p --output-format streaming-messages-json` (full assistant blocks; not
  token-delta `streaming-json`, which would break newline-joined text collection).
- `model` → `-m` / `--model` (e.g. `"grok-4.5"`). `list_models()` parses `grok models` text.
- `allowed_tools` → `--tools`; `disallowed_tools` → `--disallowed-tools` with all five canonical
  names. **Important:** `bash` maps to `run_terminal_cmd` (the working deny id). Init lists the
  shell tool as `run_terminal_command`, but denying that longer name is a no-op on grok 1.0.0.
  `edit` → `search_replace,write`.
- `effort` → `--reasoning-effort`; `auto_approve` → `--always-approve`; `include_thinking` is
  honoured from assistant thinking blocks.
- Session resume → `--resume <id>`. `system/init` and `result` carry `session_id`.
- `cost` comes from `result.total_cost_usd` (may be `0.0` on some OAuth/pool paths);
  `duration` from `duration_ms`; `output_tokens` is raw `usage.output_tokens` (reasoning is a
  subset when present — do not add `reasoning_tokens`).
- MCP via `grok mcp add|remove --scope user` only (unscoped remove can hit project config).
  `list_mcp_servers()` reads `~/.grok/config.toml`.
