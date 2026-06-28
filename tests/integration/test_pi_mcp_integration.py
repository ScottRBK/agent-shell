import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType


class TestPiMcpNotImplemented:
    # Pi manages capability via `pi install` extensions / a settings file with no documented
    # MCP subcommand; MCP support is deferred. The methods must fail loud, not silently no-op.
    async def test_add_mcp_server_raises(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx",
                             args=["forgetful-ai"])

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await shell.add_mcp_server(spec)

    async def test_remove_mcp_server_raises(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await shell.remove_mcp_server("forgetful")

    async def test_list_mcp_servers_raises(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await shell.list_mcp_servers()
