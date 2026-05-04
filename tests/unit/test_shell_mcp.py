from unittest.mock import AsyncMock

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType


class TestAgentShellMcpPassthrough:
    async def test_add_mcp_server_delegates_to_adapter(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter.add_mcp_server = AsyncMock()
        spec = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="uvx")

        # Act
        await shell.add_mcp_server(spec)

        # Assert
        shell._adapter.add_mcp_server.assert_awaited_once_with(spec)

    async def test_remove_mcp_server_delegates_to_adapter(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter.remove_mcp_server = AsyncMock()

        # Act
        await shell.remove_mcp_server("forgetful")

        # Assert
        shell._adapter.remove_mcp_server.assert_awaited_once_with("forgetful")

    async def test_list_mcp_servers_delegates_to_adapter(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        expected = [MCPServerSpec(name="x", type=MCPServerType.STDIO, command="uvx")]
        shell._adapter.list_mcp_servers = AsyncMock(return_value=expected)

        # Act
        result = await shell.list_mcp_servers()

        # Assert
        assert result is expected
        shell._adapter.list_mcp_servers.assert_awaited_once()

    async def test_add_mcp_server_propagates_exceptions(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter.add_mcp_server = AsyncMock(side_effect=RuntimeError("boom"))
        spec = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="uvx")

        # Act / Assert
        with pytest.raises(RuntimeError, match="boom"):
            await shell.add_mcp_server(spec)
