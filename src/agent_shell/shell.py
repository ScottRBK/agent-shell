import asyncio
import os
from pathlib import Path
from typing import AsyncIterator

from agent_shell.adapters.agent_adapter_protocol import AgentAdapter
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter
from agent_shell.adapters.grok_adapter import GrokAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.pi_adapter import PiAdapter
from agent_shell.execution import (
    ExecutionHost,
    IsolationPolicy,
    LinuxPidNamespaceIsolation,
    NativeExecutionHost,
    NoIsolation,
)
from agent_shell.models.agent import (
    AgentResponse,
    AgentType,
    HealthCheckResult,
    MCPServerSpec,
    StreamEvent,
)

# Every AgentType must appear here. The lookup below still guards against a member that
# does not, because adding an AgentType and forgetting the registry entry is an easy miss
# and the failure should be a clear error at construction, not an AttributeError later.
_ADAPTERS: dict[AgentType, type[AgentAdapter]] = {
        AgentType.CLAUDE_CODE: ClaudeCodeAdapter,
        AgentType.OPENCODE: OpenCodeAdapter,
        AgentType.COPILOT_CLI: CopilotCLIAdapter,
        AgentType.CODEX: CodexAdapter,
        AgentType.PI: PiAdapter,
        AgentType.CURSOR: CursorAdapter,
        AgentType.GROK: GrokAdapter,
}

_ISOLATION_POLICY_ENV = "AGENTSHELL_ISOLATION_POLICY"


def _isolation_policy_from_environment() -> IsolationPolicy:
    value = os.environ.get(_ISOLATION_POLICY_ENV)
    if value is None:
        return NoIsolation()
    if value == "none":
        return NoIsolation()
    if value == "linux-pid-namespace":
        return LinuxPidNamespaceIsolation()
    raise ValueError(
        f"Unsupported {_ISOLATION_POLICY_ENV} value {value!r}: "
        "expected 'none' or 'linux-pid-namespace'"
    )


class AgentShell():
    def __init__(
            self,
            agent_type: AgentType,
            execution_host: ExecutionHost | None = None,
            isolation_policy: IsolationPolicy | None = None,
    ):
        self.execution_host = (
            execution_host if execution_host is not None else NativeExecutionHost()
        )
        self.isolation_policy = (
            isolation_policy
            if isolation_policy is not None
            else _isolation_policy_from_environment()
        )
        self._adapter = self._resolve_adapter(agent_type=agent_type)

    def _resolve_adapter(self, agent_type: AgentType) -> AgentAdapter:
        adapter_cls = _ADAPTERS.get(agent_type)

        if not adapter_cls:
            raise ValueError(f"Unsupported agent: {agent_type}")

        return adapter_cls(
            execution_host=self.execution_host,
            isolation_policy=self.isolation_policy,
        )

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
            disallowed_tools: list[str] | None = None,
    ) -> AgentResponse:

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}")

        try:
            return await self._adapter.execute(
                    cwd=cwd,
                    prompt=prompt,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools,
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
            disallowed_tools: list[str] | None = None,
    ) -> AsyncIterator[StreamEvent]:

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}")

        try:
            async for chunk in self._adapter.stream(
                    cwd=cwd,
                    prompt=prompt,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools,
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

    async def health_check(
            self,
            cwd: str,
            model: str | None = None,
            timeout: float = 60.0,
    ) -> HealthCheckResult:

        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}")

        return await self._adapter.health_check(cwd=cwd, model=model, timeout=timeout)

    async def list_models(
            self,
            cwd: str,
            timeout: float = 30.0,
    ) -> list[str]:
        if not Path(cwd).is_dir():
            raise ValueError(f"Directory does not exist: {cwd}")

        return await self._adapter.list_models(cwd=cwd, timeout=timeout)

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        await self._adapter.add_mcp_server(mcp_server)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        await self._adapter.remove_mcp_server(mcp_server_name)

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        return await self._adapter.list_mcp_servers()
