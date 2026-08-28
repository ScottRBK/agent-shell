from dataclasses import dataclass, field
from enum import StrEnum


class AgentType(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    COPILOT_CLI = "copilot_cli"
    CODEX = "codex"
    PI = "pi"
    CURSOR = "cursor"
    GROK = "grok"

class MCPServerType(StrEnum):
    STDIO = "stdio"
    HTTP = "http"

@dataclass
class AgentResponse:
    response: str
    cost: float
    session_id: str | None = None
    duration: float = 0.0
    output_tokens: int = 0

@dataclass
class StreamEvent:
    type: str
    content: str
    cost: float = 0.0
    duration: float = 0.0
    session_id: str | None = None
    output_tokens: int = 0
    # Why a failing result failed. `content` only says "error"; this carries the reason
    # when the adapter can recover one (pi puts it in agent_end and it was being dropped).
    error: str | None = None
    # Raw process termination metadata. A signal exit follows Python's subprocess convention:
    # returncode is negative and signal contains its positive signal number.
    returncode: int | None = None
    signal: int | None = None

class AgentExecutionError(Exception):
    """Raised by `execute()` when the agent run did not succeed.

    `execute()` collapses a whole stream into one AgentResponse, so a failure carried by
    that stream had nowhere to go and simply vanished — a failed run was indistinguishable
    from a successful one that produced no text (issue #11).

    `str(e)` is the reason on its own, so a consumer that only logs the exception still
    sees the real cause (e.g. "500 model name=qwen3.6-27b-8Q failed to load"). The partial
    run data rides along so raising destroys nothing the caller already paid for: text
    produced before the failure, plus whatever cost/session/token accounting arrived.
    """

    def __init__(
            self,
            reason: str,
            response: str = "",
            cost: float = 0.0,
            session_id: str | None = None,
            duration: float = 0.0,
            output_tokens: int = 0,
            returncode: int | None = None,
            signal: int | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.response = response
        self.cost = cost
        self.session_id = session_id
        self.duration = duration
        self.output_tokens = output_tokens
        self.returncode = returncode
        self.signal = signal


@dataclass
class HealthCheckResult:
    healthy: bool
    exception: str | None = None

@dataclass
class MCPServerSpec:
    name: str
    type: MCPServerType
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.type == MCPServerType.STDIO:
            if not self.command:
                raise ValueError("STDIO MCP servers require 'command'")
            if self.url:
                raise ValueError("STDIO MCP servers cannot have 'url'")
            if self.headers:
                raise ValueError("STDIO MCP servers cannot have 'headers'")
        elif self.type == MCPServerType.HTTP:
            if not self.url:
                raise ValueError("HTTP MCP servers require 'url'")
            if self.command:
                raise ValueError("HTTP MCP servers cannot have 'command'")
            if self.args:
                raise ValueError("HTTP MCP servers cannot have 'args'")
            if self.env:
                raise ValueError("HTTP MCP servers cannot have 'env'")
