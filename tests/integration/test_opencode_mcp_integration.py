import json
from pathlib import Path

import pytest

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.models.agent import MCPServerSpec, MCPServerType


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _read_config(home: Path) -> dict:
    config_path = home / ".config" / "opencode" / "opencode.json"
    return json.loads(config_path.read_text())


class TestAddMcpServerStdio:
    async def test_writes_local_entry_to_user_config(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()
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
        config = _read_config(isolated_home)
        entry = config["mcp"]["forgetful"]
        assert entry["type"] == "local"
        assert entry["command"] == ["uvx", "forgetful-ai"]
        assert entry["environment"] == {"FORGETFUL_API_KEY": "secret"}
        assert entry["enabled"] is True

    async def test_creates_config_file_and_parent_dir(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")

        # Act
        await adapter.add_mcp_server(spec)

        # Assert
        config_path = isolated_home / ".config" / "opencode" / "opencode.json"
        assert config_path.exists()

    async def test_preserves_existing_unrelated_config(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".config" / "opencode" / "opencode.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"model": "claude-opus", "theme": "dark"}))

        adapter = OpenCodeAdapter()
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")

        # Act
        await adapter.add_mcp_server(spec)

        # Assert
        config = _read_config(isolated_home)
        assert config["model"] == "claude-opus"
        assert config["theme"] == "dark"
        assert "forgetful" in config["mcp"]

    async def test_overwrites_existing_server_with_same_name(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()
        old = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="old-cmd")
        new = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="new-cmd")

        # Act
        await adapter.add_mcp_server(old)
        await adapter.add_mcp_server(new)

        # Assert
        config = _read_config(isolated_home)
        assert config["mcp"]["x"]["command"] == ["new-cmd"]


class TestAddMcpServerHttp:
    async def test_writes_remote_entry(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer x"},
        )

        # Act
        await adapter.add_mcp_server(spec)

        # Assert
        entry = _read_config(isolated_home)["mcp"]["remote"]
        assert entry["type"] == "remote"
        assert entry["url"] == "https://example.com/mcp"
        assert entry["headers"] == {"Authorization": "Bearer x"}
        assert entry["enabled"] is True


class TestRemoveMcpServer:
    async def test_removes_existing_entry(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")
        await adapter.add_mcp_server(spec)

        # Act
        await adapter.remove_mcp_server("forgetful")

        # Assert
        config = _read_config(isolated_home)
        assert "forgetful" not in config.get("mcp", {})

    async def test_warns_when_server_not_found(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act / Assert
        with pytest.warns(UserWarning, match="missing"):
            await adapter.remove_mcp_server("missing")

    async def test_warns_when_config_file_missing(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act / Assert (no config file at all)
        with pytest.warns(UserWarning, match="missing"):
            await adapter.remove_mcp_server("missing")


class TestListMcpServers:
    async def test_returns_empty_when_no_config(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        servers = await adapter.list_mcp_servers()

        # Assert
        assert servers == []

    async def test_round_trips_stdio_spec(self, isolated_home):
        # Arrange
        adapter = OpenCodeAdapter()
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
        adapter = OpenCodeAdapter()
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
