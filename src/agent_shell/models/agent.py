from enum import StrEnum
from dataclasses import dataclass, field

class AgentType(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    GEMINI_CLI = "gemini_cli"
    COPILOT_CLI = "copilot_cli"
    CODEX = "codex"

class MCPServerType(StrEnum):
    STDIO = "stdio"
    HTTP = "http"

@dataclass
class AgentResponse:
    response: str
    cost: float
    session_id: str | None = None
    duration: float = 0.0

@dataclass
class StreamEvent:
    type: str
    content: str
    cost: float = 0.0
    duration: float = 0.0
    session_id: str | None = None

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
