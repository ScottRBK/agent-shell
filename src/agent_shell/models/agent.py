from enum import StrEnum
from dataclasses import dataclass

class AgentType(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    GEMINI_CLI = "gemini_cli"
    COPILOT_CLI = "copilot_cli"
    CODEX = "codex"

@dataclass
class AgentResponse:
    response: str
    cost: float
    session_id: str | None = None

@dataclass
class StreamEvent:
    type: str
    content: str
    cost: float = 0.0
    duration: float = 0.0
    session_id: str | None = None

