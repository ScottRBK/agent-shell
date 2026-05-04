import asyncio
from pathlib import Path
from typing import AsyncIterator

from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent, MCPServerSpec
from agent_shell.adapters.agent_adapter_protocol import AgentAdapter
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter


class AgentShell():
    def __init__(self, agent_type: AgentType):
        self._adapter = self._resolve_adapter(agent_type=agent_type)

    def _resolve_adapter(self, agent_type: AgentType) -> AgentAdapter:
        adapters = {
                AgentType.CLAUDE_CODE: ClaudeCodeAdapter,
                AgentType.OPENCODE: OpenCodeAdapter,
                AgentType.COPILOT_CLI: CopilotCLIAdapter,
        }
        
        adapter_cls = adapters.get(agent_type)

        if not adapter_cls:
            raise ValueError(f"Unsupported agent: {agent_type}")

        return adapter_cls()

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

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}")

        try:
            return await self._adapter.execute(
                    cwd=cwd,
                    prompt=prompt,
                    allowed_tools=allowed_tools,
                    model=model,
                    effort=effort,
                    include_thinking=include_thinking,
                    auto_approve=auto_approve,
                    session_id=session_id,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            await self._adapter.cancel()
            raise

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
    ) -> AsyncIterator[StreamEvent]:

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}")

        try:
            async for chunk in self._adapter.stream(
                    cwd=cwd,
                    prompt=prompt,
                    allowed_tools=allowed_tools,
                    model=model,
                    effort=effort,
                    include_thinking=include_thinking,
                    auto_approve=auto_approve,
                    session_id=session_id,
            ):
                yield chunk
        except (KeyboardInterrupt, asyncio.CancelledError):
            await self._adapter.cancel()
            raise

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        await self._adapter.add_mcp_server(mcp_server)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        await self._adapter.remove_mcp_server(mcp_server_name)

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        return await self._adapter.list_mcp_servers()
