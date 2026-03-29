from pathlib import Path 
from typing import AsyncIterator

from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent
from agent_shell.adapters.agent_adapter_protocol import AgentAdapter
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter


class AgentShell():
    def __init__(self, agent_type: AgentType):
        self._adapter = self._resolve_adapter(agent_type=agent_type)

    def _resolve_adapter(self, agent_type: AgentType) -> AgentAdapter:
        adapters = {
                AgentType.CLAUDE_CODE: ClaudeCodeAdapter
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
            include_thinking: bool = False,
    ) -> AgentResponse:

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}") 

        try: 
            return await self._adapter.execute(
                    cwd=cwd,
                    prompt=prompt,
                    allowed_tools=allowed_tools,
                    model=model
            )
        except KeyboardInterrupt:
            await self._adapter.cancel()
            raise

    async def stream(
            self,
            cwd: str, 
            prompt: str,
            allowed_tools: list[str] | None = None,
            model: str | None = None,
            include_thinking: bool = False,
    ) -> AsyncIterator[StreamEvent]:

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}") 
        
        try:
            async for chunk in self._adapter.stream(
                    cwd=cwd,
                    prompt=prompt,
                    allowed_tools=allowed_tools,
                    model=model,
            ):
                yield chunk
        except KeyboardInterrupt:
            await self._adapter.cancel()
            raise

