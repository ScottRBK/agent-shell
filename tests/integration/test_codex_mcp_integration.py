import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.models.agent import MCPServerSpec, MCPServerType

from tests.unit.codex_fixtures import MCP_LIST_OUTPUT, MCP_LIST_EMPTY


def _make_mock_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    process = AsyncMock()
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.returncode = returncode
    process.pid = 12345
    return process


class TestAddMcpServerStdio:
    async def test_invokes_codex_mcp_add_for_stdio(self):
        # Arrange
        adapter = CodexAdapter()
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
            env={"K": "V"},
        )
        mock_process = _make_mock_process()

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.add_mcp_server(spec)

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "codex"
        assert cmd_args[1] == "mcp"
        assert cmd_args[2] == "add"
        assert "forgetful" in cmd_args
        assert "--env" in cmd_args
        assert "K=V" in cmd_args
        # The cmd + args appear after `--`
        dash_idx = cmd_args.index("--")
        assert cmd_args[dash_idx + 1] == "uvx"
        assert cmd_args[dash_idx + 2] == "forgetful-ai"

    async def test_raises_when_codex_mcp_add_fails(self):
        # Arrange
        adapter = CodexAdapter()
        spec = MCPServerSpec(
            name="x", type=MCPServerType.STDIO, command="cmd"
        )
        mock_process = _make_mock_process(returncode=1, stderr=b"bad config")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="bad config"):
                await adapter.add_mcp_server(spec)


class TestAddMcpServerHttp:
    async def test_invokes_codex_mcp_add_for_http(self):
        # Arrange
        adapter = CodexAdapter()
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
        assert cmd_args[0] == "codex"
        assert cmd_args[1] == "mcp"
        assert cmd_args[2] == "add"
        assert "remote" in cmd_args
        assert "--url" in cmd_args
        assert cmd_args[cmd_args.index("--url") + 1] == "https://example.com/mcp"

    async def test_warns_when_http_spec_has_headers(self):
        # Arrange
        adapter = CodexAdapter()
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer x"},
        )
        mock_process = _make_mock_process()

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="headers"):
                await adapter.add_mcp_server(spec)


class TestRemoveMcpServer:
    async def test_invokes_codex_mcp_remove(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process(stdout=b"Removed global MCP server 'foo'.\n")

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.remove_mcp_server("foo")

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args == ("codex", "mcp", "remove", "foo")

    async def test_warns_when_server_not_found(self):
        # Arrange
        adapter = CodexAdapter()
        # codex returns exit 0 with this message when the server doesn't exist
        mock_process = _make_mock_process(
            returncode=0, stdout=b"No MCP server named 'missing' found.\n"
        )

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="missing"):
                await adapter.remove_mcp_server("missing")


class TestListMcpServers:
    async def test_returns_empty_when_no_servers(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process(stdout=MCP_LIST_EMPTY.encode())

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            servers = await adapter.list_mcp_servers()

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args == ("codex", "mcp", "list", "--json")
        assert servers == []

    async def test_round_trips_stdio_and_http_specs(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process(stdout=MCP_LIST_OUTPUT.encode())

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            servers = await adapter.list_mcp_servers()

        # Assert
        by_name = {s.name: s for s in servers}
        assert "agentshell_spike_stdio" in by_name
        assert "agentshell_spike_http" in by_name

        stdio = by_name["agentshell_spike_stdio"]
        assert stdio.type == MCPServerType.STDIO
        assert stdio.command == "/usr/bin/echo"
        assert stdio.args == ["hello", "world"]
        assert stdio.env == {"FOO": "bar", "BAZ": "qux"}

        http = by_name["agentshell_spike_http"]
        assert http.type == MCPServerType.HTTP
        assert http.url == "https://example.com/mcp"

    async def test_skips_unknown_transport_type_with_warning(self):
        # Arrange
        adapter = CodexAdapter()
        bad_payload = json.dumps([
            {
                "name": "weird",
                "transport": {"type": "carrier_pigeon"},
            },
            {
                "name": "good",
                "transport": {
                    "type": "stdio",
                    "command": "/bin/true",
                    "args": [],
                    "env": None,
                },
            },
        ])
        mock_process = _make_mock_process(stdout=bad_payload.encode())

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="weird"):
                servers = await adapter.list_mcp_servers()

        # Assert
        assert [s.name for s in servers] == ["good"]

    async def test_raises_when_codex_mcp_list_fails(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process(returncode=1, stderr=b"codex broken")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="broken"):
                await adapter.list_mcp_servers()
