import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType


class TestCursorMcpNotImplemented:
    # Cursor's `mcp` subcommands are login/list/list-tools/enable/disable ONLY — no add/remove.
    # Servers are declared in .cursor/mcp.json, and `mcp list` reports only `name: status`
    # (not the transport/command/url MCPServerSpec needs), so no method can be faithfully
    # supported. They must fail loud, not silently no-op.
    async def test_add_mcp_server_raises(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx",
                             args=["forgetful-ai"])

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await shell.add_mcp_server(spec)

    async def test_remove_mcp_server_raises(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await shell.remove_mcp_server("forgetful")

    async def test_list_mcp_servers_raises(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await shell.list_mcp_servers()
