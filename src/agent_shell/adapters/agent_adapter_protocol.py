from typing import Protocol, AsyncIterator
from agent_shell.models.agent import AgentResponse, StreamEvent

class AgentAdapter(Protocol):
    async def execute(
            self, 
            cwd: str, 
            prompt: str, 
            allowed_tools: list[str] | None = None, 
            model: str | None = None,
            include_thinking: bool = False,
    ) -> AgentResponse:
        ...

    def stream(
            self, 
            cwd: str, 
            prompt: str, 
            allowed_tools: list[str] | None = None, 
            model: str | None = None,
            include_thinking: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        ...

    async def cancel(self) -> None:
        ...
