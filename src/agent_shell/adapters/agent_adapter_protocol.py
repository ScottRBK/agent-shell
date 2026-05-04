from typing import Protocol, AsyncIterator
from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec

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
            session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        ...

    async def cancel(self) -> None:
        ...

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        ...

    async def remove_mcp_server(self, mcp_server_name: str) -> None: 
        ...

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        ...
