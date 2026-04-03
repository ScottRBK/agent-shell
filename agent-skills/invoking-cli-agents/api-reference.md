# AgentShell API Reference

## Models

### AgentType

```python
from agent_shell.models.agent import AgentType

class AgentType(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    GEMINI_CLI = "gemini_cli"   # No adapter yet
    COPILOT_CLI = "copilot_cli" # No adapter yet
    CODEX = "codex"             # No adapter yet
```

### AgentResponse

Returned by `execute()`.

```python
@dataclass
class AgentResponse:
    response: str           # Full text output from the agent
    cost: float             # Total API cost in USD
    session_id: str | None  # Use to resume this conversation
```

### StreamEvent

Yielded by `stream()`.

```python
@dataclass
class StreamEvent:
    type: str              # Event type (see table below)
    content: str           # Event payload
    cost: float            # Cumulative cost in USD (populated on "result" events)
    duration: float        # Elapsed time in seconds (populated on "result" events)
    session_id: str | None # Present on "system" and "result" events
```

#### Event Types

| Type | When | Content | Other fields |
|------|------|---------|--------------|
| `system` | Session starts | `""` (empty string) | `session_id` is set |
| `text` | Agent produces output | The agent's response text (may arrive in chunks) | |
| `thinking` | Agent reasons (Claude Code + `stream()` only, requires `include_thinking=True`) | Chain-of-thought text | |
| `tool_use` | Agent invokes a tool | Tool name only (no arguments) | |
| `result` | Agent finishes | `"ok"` on success, `"error"` on Claude Code agent-level failure | `cost` and `duration` are populated. `duration` is only set by Claude Code. OpenCode emits failures as `error` events instead. |
| `error` | Agent or CLI process fails | Error message or stderr output (last 500 chars) | OpenCode emits errors as this event type, not as `result`. |

## AgentShell Class

```python
from agent_shell.shell import AgentShell

class AgentShell:
    def __init__(self, agent_type: AgentType): ...

    async def execute(
        self,
        cwd: str,                              # Working directory (must exist)
        prompt: str,                           # Task for the agent
        allowed_tools: list[str] | None = None,# Tool whitelist (None = all)
        model: str | None = None,              # Model alias or full ID
        effort: str | None = None,             # "low" | "medium" | "high" | "max"
        include_thinking: bool = False,        # Include chain-of-thought
        auto_approve: bool = True,             # Skip tool permission prompts
        session_id: str | None = None,         # Resume previous session
    ) -> AgentResponse: ...

    async def stream(
        self,
        cwd: str,
        prompt: str,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        include_thinking: bool = False,
        auto_approve: bool = True,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
```

## AgentAdapter Protocol

To add support for a new CLI agent, implement this protocol (structural typing - no inheritance required):

```python
from typing import Protocol, AsyncIterator
from agent_shell.models.agent import AgentResponse, StreamEvent

class AgentAdapter(Protocol):
    async def execute(
        self,
        cwd: str,
        prompt: str,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        include_thinking: bool = False,
        auto_approve: bool = True,
        session_id: str | None = None,
    ) -> AgentResponse: ...

    def stream(
        self,
        cwd: str,
        prompt: str,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        include_thinking: bool = False,
        auto_approve: bool = True,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def cancel(self) -> None: ...
```

## Agent-Specific Notes

### Claude Code

- `model` accepts aliases like `"sonnet"`, `"opus"`, or full IDs like `"claude-sonnet-4-6"`
- `allowed_tools` maps to Claude Code tool names: `"Read"`, `"Edit"`, `"Write"`, `"Bash"`, `"Glob"`, `"Grep"`, `"Agent"`, etc.
- `effort` maps to `--effort` flag
- `include_thinking` does **not** add a CLI flag. It only controls whether thinking events already present in the streamed output are yielded or filtered out. Claude Code may include thinking content in its output by default depending on the model.
- Output is NDJSON with event types: `system`, `assistant` (parsed into text/tool_use/thinking), `result`

### OpenCode

- `model` uses provider-prefixed names like `"anthropic/claude-sonnet-4-5"` or `"github-copilot/gpt-5.4"`
- **Only `model` and `session_id` are mapped to CLI flags.** `allowed_tools`, `effort`, `include_thinking`, and `auto_approve` are accepted by the adapter signature but silently ignored.
- OpenCode's `run` mode auto-approves all tools unconditionally — there is no way to restrict tool access via AgentShell.
- `duration` is not populated on `result` events (always `0.0`).
- Output is NDJSON with event types: `step_start`, `text`, `tool_use`, `step_finish`, `error`
