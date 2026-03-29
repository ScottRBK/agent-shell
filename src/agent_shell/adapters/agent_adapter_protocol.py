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
    ) -> AgentResponse:
        ...

    def stream(
            self,
            cwd: str,
            prompt: str,
            allowed_tools: list[str] | None = None,
            model: str | None = None,
            effort: str | None = None,
            include_thinking: bool = False,
            auto_approve: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        ...

    async def cancel(self) -> None:
        ...
