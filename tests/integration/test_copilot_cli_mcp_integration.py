import json
from pathlib import Path

import pytest

from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.models.agent import MCPServerSpec, MCPServerType


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _read_config(home: Path) -> dict:
    return json.loads((home / ".copilot" / "mcp-config.json").read_text())


class TestAddMcpServerStdio:
    async def test_writes_local_entry_to_user_config(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
            env={"FORGETFUL_API_KEY": "secret"},
        )

        # Act
        await adapter.add_mcp_server(spec)

        # Assert
        entry = _read_config(isolated_home)["mcpServers"]["forgetful"]
        assert entry["type"] == "local"
        assert entry["command"] == "uvx"
        assert entry["args"] == ["forgetful-ai"]
        assert entry["env"] == {"FORGETFUL_API_KEY": "secret"}

    async def test_creates_config_file_and_parent_dir(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")

        # Act
        await adapter.add_mcp_server(spec)

        # Assert
        config_path = isolated_home / ".copilot" / "mcp-config.json"
        assert config_path.exists()

    async def test_preserves_existing_servers(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        await adapter.add_mcp_server(
            MCPServerSpec(name="other", type=MCPServerType.STDIO, command="other-cmd")
        )

        # Act
        await adapter.add_mcp_server(
            MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")
        )

        # Assert
        servers = _read_config(isolated_home)["mcpServers"]
        assert "other" in servers
        assert "forgetful" in servers

    async def test_overwrites_existing_server_with_same_name(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        old = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="old")
        new = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="new")

        # Act
        await adapter.add_mcp_server(old)
        await adapter.add_mcp_server(new)

        # Assert
        assert _read_config(isolated_home)["mcpServers"]["x"]["command"] == "new"


class TestAddMcpServerHttp:
    async def test_writes_http_entry(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer x"},
        )

        # Act
        await adapter.add_mcp_server(spec)

        # Assert
        entry = _read_config(isolated_home)["mcpServers"]["remote"]
        assert entry["type"] == "http"
        assert entry["url"] == "https://example.com/mcp"
        assert entry["headers"] == {"Authorization": "Bearer x"}


class TestRemoveMcpServer:
    async def test_removes_existing_entry(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        await adapter.add_mcp_server(
            MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")
        )

        # Act
        await adapter.remove_mcp_server("forgetful")

        # Assert
        assert "forgetful" not in _read_config(isolated_home).get("mcpServers", {})

    async def test_warns_when_server_not_found(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act / Assert
        with pytest.warns(UserWarning, match="missing"):
            await adapter.remove_mcp_server("missing")

    async def test_warns_when_config_file_missing(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act / Assert
        with pytest.warns(UserWarning, match="missing"):
            await adapter.remove_mcp_server("missing")


class TestListMcpServers:
    async def test_returns_empty_when_no_config(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        servers = await adapter.list_mcp_servers()

        # Assert
        assert servers == []

    async def test_round_trips_stdio_spec(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
            env={"K": "V"},
        )
        await adapter.add_mcp_server(spec)

        # Act
        servers = await adapter.list_mcp_servers()

        # Assert
        assert len(servers) == 1
        listed = servers[0]
        assert listed.name == "forgetful"
        assert listed.type == MCPServerType.STDIO
        assert listed.command == "uvx"
        assert listed.args == ["forgetful-ai"]
        assert listed.env == {"K": "V"}

    async def test_round_trips_http_spec(self, isolated_home):
        # Arrange
        adapter = CopilotCLIAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer x"},
        )
        await adapter.add_mcp_server(spec)

        # Act
        servers = await adapter.list_mcp_servers()

        # Assert
        assert len(servers) == 1
        listed = servers[0]
        assert listed.name == "remote"
        assert listed.type == MCPServerType.HTTP
        assert listed.url == "https://example.com/mcp"
        assert listed.headers == {"Authorization": "Bearer x"}
