import pytest
from unittest.mock import AsyncMock, patch

from agent_shell.adapters.grok_adapter import GrokAdapter
from agent_shell.models.agent import MCPServerSpec, MCPServerType


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_mock_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    process = AsyncMock()
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.returncode = returncode
    process.pid = 12345
    return process


class TestAddMcpServerStdio:
    async def test_invokes_grok_mcp_add_with_user_scope(self):
        # Arrange
        adapter = GrokAdapter()
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
        )
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert — single add call (add is upsert; no pre-remove that could hit project scope).
        assert mock_exec.call_count == 1
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0:3] == ("grok", "mcp", "add")
        assert cmd_args[cmd_args.index("--scope") + 1] == "user"
        assert cmd_args[cmd_args.index("--transport") + 1] == "stdio"
        assert "forgetful" in cmd_args
        sep_idx = cmd_args.index("--")
        assert cmd_args[sep_idx + 1] == "uvx"
        assert cmd_args[sep_idx + 2] == "forgetful-ai"

    async def test_add_does_not_pre_remove(self):
        # Arrange — regression: unscoped remove searched project config too.
        adapter = GrokAdapter()
        spec = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="uvx")
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        assert mock_exec.call_count == 1
        assert mock_exec.call_args[0][0:3] == ("grok", "mcp", "add")

    async def test_passes_env_vars_with_e_flag(self):
        # Arrange
        adapter = GrokAdapter()
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
            env={"FORGETFUL_API_KEY": "secret", "FORGETFUL_URL": "http://x"},
        )
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        cmd_args = mock_exec.call_args[0]
        e_indices = [i for i, v in enumerate(cmd_args) if v == "-e"]
        assert len(e_indices) == 2
        env_values = {cmd_args[i + 1] for i in e_indices}
        assert "FORGETFUL_API_KEY=secret" in env_values
        assert "FORGETFUL_URL=http://x" in env_values

    async def test_raises_on_subprocess_failure(self):
        # Arrange
        adapter = GrokAdapter()
        spec = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="uvx")
        add_fail = _make_mock_process(returncode=1, stderr=b"bad config")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=add_fail):
            with pytest.raises(RuntimeError, match="bad config"):
                await adapter.add_mcp_server(spec)


class TestAddMcpServerHttp:
    async def test_invokes_grok_mcp_add_with_http_transport(self):
        # Arrange
        adapter = GrokAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
        )
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--transport") + 1] == "http"
        assert "remote" in cmd_args
        assert "https://example.com/mcp" in cmd_args
        assert "--" not in cmd_args

    async def test_passes_headers(self):
        # Arrange
        adapter = GrokAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer tok"},
        )
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--header" in cmd_args
        assert cmd_args[cmd_args.index("--header") + 1] == "Authorization: Bearer tok"


class TestRemoveMcpServer:
    async def test_invokes_grok_mcp_remove_with_user_scope(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.remove_mcp_server("forgetful")

        # Assert — user scope is mandatory so project config is never touched.
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0:3] == ("grok", "mcp", "remove")
        assert cmd_args[cmd_args.index("--scope") + 1] == "user"
        assert "forgetful" in cmd_args

    async def test_warns_when_remove_fails(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process(returncode=1, stderr=b"not found")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="Could not remove MCP server"):
                await adapter.remove_mcp_server("missing")


class TestListMcpServers:
    async def test_reads_stdio_and_http_from_user_config(self, isolated_home):
        # Arrange — real TOML shape written by `grok mcp add`.
        config_dir = isolated_home / ".grok"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            """
[mcp_servers.forgetful]
command = "uvx"
args = ["forgetful-ai"]
enabled = true

[mcp_servers.forgetful.env]
FORGETFUL_API_KEY = "secret"

[mcp_servers.remote]
url = "https://example.com/mcp"
enabled = true

[mcp_servers.remote.headers]
Authorization = "Bearer tok"
""".strip()
            + "\n",
            encoding="utf-8",
        )
        adapter = GrokAdapter()

        # Act
        servers = await adapter.list_mcp_servers()

        # Assert
        by_name = {s.name: s for s in servers}
        assert set(by_name) == {"forgetful", "remote"}

        stdio = by_name["forgetful"]
        assert stdio.type == MCPServerType.STDIO
        assert stdio.command == "uvx"
        assert stdio.args == ["forgetful-ai"]
        assert stdio.env == {"FORGETFUL_API_KEY": "secret"}

        http = by_name["remote"]
        assert http.type == MCPServerType.HTTP
        assert http.url == "https://example.com/mcp"
        assert http.headers == {"Authorization": "Bearer tok"}

    async def test_returns_empty_when_config_missing(self, isolated_home):
        # Arrange
        adapter = GrokAdapter()

        # Act
        servers = await adapter.list_mcp_servers()

        # Assert
        assert servers == []
