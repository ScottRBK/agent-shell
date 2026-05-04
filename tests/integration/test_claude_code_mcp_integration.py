import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.models.agent import MCPServerSpec, MCPServerType


def _make_mock_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    process = AsyncMock()
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.returncode = returncode
    process.pid = 12345
    return process


class TestAddMcpServerStdio:
    async def test_invokes_claude_mcp_add_with_user_scope(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
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

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "claude"
        assert cmd_args[1] == "mcp"
        assert cmd_args[2] == "add"
        assert "--scope" in cmd_args
        assert cmd_args[cmd_args.index("--scope") + 1] == "user"
        assert "--transport" in cmd_args
        assert cmd_args[cmd_args.index("--transport") + 1] == "stdio"
        assert "forgetful" in cmd_args
        assert "--" in cmd_args
        sep_idx = cmd_args.index("--")
        assert cmd_args[sep_idx + 1] == "uvx"
        assert cmd_args[sep_idx + 2] == "forgetful-ai"

    async def test_passes_env_vars_with_e_flag(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
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
        adapter = ClaudeCodeAdapter()
        spec = MCPServerSpec(name="x", type=MCPServerType.STDIO, command="uvx")
        mock_process = _make_mock_process(returncode=1, stderr=b"already exists")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="already exists"):
                await adapter.add_mcp_server(spec)


class TestAddMcpServerHttp:
    async def test_invokes_claude_mcp_add_with_http_transport(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
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
        assert "--transport" in cmd_args
        assert cmd_args[cmd_args.index("--transport") + 1] == "http"
        assert "remote" in cmd_args
        assert "https://example.com/mcp" in cmd_args
        assert "--" not in cmd_args  # no command separator for HTTP

    async def test_passes_headers_with_H_flag(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer abc", "X-Trace": "1"},
        )
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        cmd_args = mock_exec.call_args[0]
        h_indices = [i for i, v in enumerate(cmd_args) if v == "--header"]
        assert len(h_indices) == 2
        header_values = {cmd_args[i + 1] for i in h_indices}
        assert "Authorization: Bearer abc" in header_values
        assert "X-Trace: 1" in header_values


class TestRemoveMcpServer:
    async def test_invokes_claude_mcp_remove_with_user_scope(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.remove_mcp_server("forgetful")

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "claude"
        assert cmd_args[1] == "mcp"
        assert cmd_args[2] == "remove"
        assert "--scope" in cmd_args
        assert cmd_args[cmd_args.index("--scope") + 1] == "user"
        assert "forgetful" in cmd_args

    async def test_warns_if_server_not_found(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        mock_process = _make_mock_process(
            returncode=1, stderr=b"No MCP server found with name: missing"
        )

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="missing"):
                await adapter.remove_mcp_server("missing")


class TestListMcpServers:
    async def test_raises_not_implemented(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act / Assert
        with pytest.raises(NotImplementedError):
            await adapter.list_mcp_servers()


class TestOverwriteSemantics:
    async def test_add_calls_remove_then_add_when_server_exists(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        spec = MCPServerSpec(name="forgetful", type=MCPServerType.STDIO, command="uvx")

        # First call (remove) succeeds; second call (add) succeeds.
        # add_mcp_server should pre-remove to make adds idempotent.
        remove_proc = _make_mock_process(returncode=0)
        add_proc = _make_mock_process(returncode=0)

        # Act
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=[remove_proc, add_proc],
        ) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        assert mock_exec.call_count == 2
        first_call_args = mock_exec.call_args_list[0][0]
        second_call_args = mock_exec.call_args_list[1][0]
        assert first_call_args[2] == "remove"
        assert second_call_args[2] == "add"
