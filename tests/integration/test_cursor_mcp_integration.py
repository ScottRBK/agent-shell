import json
import stat
from pathlib import Path

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _read_config(home: Path) -> dict:
    return json.loads((home / ".cursor" / "mcp.json").read_text())


class TestCursorMcp:
    async def test_adds_stdio_server_to_user_config(self, isolated_home):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
            env={"FORGETFUL_API_KEY": "secret"},
        )

        # Act
        await shell.add_mcp_server(spec)

        # Assert
        config_path = isolated_home / ".cursor" / "mcp.json"
        assert _read_config(isolated_home) == {
            "mcpServers": {
                "forgetful": {
                    "command": "uvx",
                    "args": ["forgetful-ai"],
                    "env": {"FORGETFUL_API_KEY": "secret"},
                }
            }
        }
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    async def test_failed_atomic_replace_preserves_existing_config(
            self,
            isolated_home,
            monkeypatch,
    ):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        original = {"mcpServers": {"existing": {"command": "existing-command"}}}
        config_path.write_text(json.dumps(original))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        def fail_replace(source, destination):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("agent_shell.adapters.cursor_adapter.os.replace", fail_replace)

        # Act / Assert
        with pytest.raises(OSError, match="simulated replace failure"):
            await shell.add_mcp_server(MCPServerSpec(
                name="new",
                type=MCPServerType.STDIO,
                command="new-command",
            ))

        assert _read_config(isolated_home) == original
        assert list(config_path.parent.iterdir()) == [config_path]

    async def test_add_rejects_malformed_server_collection_without_writing(
            self,
            isolated_home,
    ):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        original = {"mcpServers": ["broken"]}
        config_path.write_text(json.dumps(original))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act / Assert
        with pytest.raises(ValueError, match="mcpServers"):
            await shell.add_mcp_server(MCPServerSpec(
                name="new",
                type=MCPServerType.STDIO,
                command="new-command",
            ))

        assert _read_config(isolated_home) == original

    async def test_same_transport_update_preserves_cursor_only_fields(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "local": {
                    "type": "stdio",
                    "command": "old-command",
                    "args": ["old-arg"],
                    "env": {"OLD": "value"},
                    "envFile": "~/.cursor/mcp.env",
                }
            }
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)
        replacement = MCPServerSpec(
            name="local",
            type=MCPServerType.STDIO,
            command="new-command",
            args=["new-arg"],
            env={"NEW": "value"},
        )

        # Act
        await shell.add_mcp_server(replacement)

        # Assert
        assert _read_config(isolated_home)["mcpServers"]["local"] == {
            "type": "stdio",
            "command": "new-command",
            "args": ["new-arg"],
            "env": {"NEW": "value"},
            "envFile": "~/.cursor/mcp.env",
        }

    async def test_remote_update_preserves_auth_and_sse_type(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "remote": {
                    "type": "sse",
                    "url": "https://old.example.com/sse",
                    "headers": {"OLD": "value"},
                    "auth": {"CLIENT_ID": "client-id"},
                }
            }
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)
        replacement = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://new.example.com/sse",
            headers={"NEW": "value"},
        )

        # Act
        await shell.add_mcp_server(replacement)

        # Assert
        assert _read_config(isolated_home)["mcpServers"]["remote"] == {
            "type": "sse",
            "url": "https://new.example.com/sse",
            "headers": {"NEW": "value"},
            "auth": {"CLIENT_ID": "client-id"},
        }

    async def test_transport_change_drops_old_transport_fields(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "changing": {
                    "type": "stdio",
                    "command": "old-command",
                    "envFile": "~/.cursor/mcp.env",
                }
            }
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        await shell.add_mcp_server(MCPServerSpec(
            name="changing",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
        ))

        # Assert
        assert _read_config(isolated_home)["mcpServers"]["changing"] == {
            "url": "https://example.com/mcp",
            "headers": {},
        }

    async def test_adds_http_server_without_replacing_existing_config(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {"existing": {"command": "existing-command"}},
            "unrelated": {"preserve": True},
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer secret"},
        )

        # Act
        await shell.add_mcp_server(spec)

        # Assert
        assert _read_config(isolated_home) == {
            "mcpServers": {
                "existing": {"command": "existing-command"},
                "remote": {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer secret"},
                },
            },
            "unrelated": {"preserve": True},
        }

    async def test_removes_server_without_replacing_existing_config(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "forgetful": {"command": "uvx"},
                "existing": {"command": "existing-command"},
            },
            "unrelated": {"preserve": True},
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        await shell.remove_mcp_server("forgetful")

        # Assert
        assert _read_config(isolated_home) == {
            "mcpServers": {"existing": {"command": "existing-command"}},
            "unrelated": {"preserve": True},
        }

    async def test_remove_warns_when_server_is_missing(self, isolated_home):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act / Assert
        with pytest.warns(UserWarning, match="not found"):
            await shell.remove_mcp_server("missing")

        assert not (isolated_home / ".cursor" / "mcp.json").exists()

    async def test_lists_stdio_and_http_servers_from_user_config(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "forgetful": {
                    "command": "uvx",
                    "args": ["forgetful-ai"],
                    "env": {"K": "V"},
                },
                "remote": {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer secret"},
                },
            }
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        servers = await shell.list_mcp_servers()

        # Assert
        assert servers == [
            MCPServerSpec(
                name="forgetful",
                type=MCPServerType.STDIO,
                command="uvx",
                args=["forgetful-ai"],
                env={"K": "V"},
            ),
            MCPServerSpec(
                name="remote",
                type=MCPServerType.HTTP,
                url="https://example.com/mcp",
                headers={"Authorization": "Bearer secret"},
            ),
        ]

    async def test_list_honours_explicit_transport_type(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "legacy-sse": {
                    "type": "sse",
                    "url": "https://example.com/sse",
                    "command": "must-not-win",
                }
            }
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        servers = await shell.list_mcp_servers()

        # Assert
        assert servers == [MCPServerSpec(
            name="legacy-sse",
            type=MCPServerType.HTTP,
            url="https://example.com/sse",
        )]

    async def test_list_skips_malformed_entries_with_warnings(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "mcpServers": {
                "not-an-object": "broken",
                "missing-transport": {"args": ["orphaned"]},
                "invalid-args": {"command": "uvx", "args": 42},
                "string-args": {"command": "uvx", "args": "--port"},
                "invalid-env": {"command": "uvx", "env": {"PORT": 42}},
                "good": {"command": "good-command"},
            }
        }))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        with pytest.warns(UserWarning) as captured:
            servers = await shell.list_mcp_servers()

        # Assert
        messages = [str(warning.message) for warning in captured]
        assert any("not-an-object" in message for message in messages)
        assert any("missing-transport" in message for message in messages)
        assert any("invalid-args" in message for message in messages)
        assert any("string-args" in message for message in messages)
        assert any("invalid-env" in message for message in messages)
        assert servers == [MCPServerSpec(
            name="good",
            type=MCPServerType.STDIO,
            command="good-command",
        )]

    async def test_list_warns_when_mcp_servers_is_not_an_object(self, isolated_home):
        # Arrange
        config_path = isolated_home / ".cursor" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"mcpServers": ["broken"]}))
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        with pytest.warns(UserWarning, match="mcpServers"):
            servers = await shell.list_mcp_servers()

        # Assert
        assert servers == []
