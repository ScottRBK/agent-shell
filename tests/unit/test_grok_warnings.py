import json
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.adapters.grok_adapter import GrokAdapter

from tests.unit.grok_fixtures import RESULT_SUCCESS_EVENT


def _make_mock_process(ndjson_lines: list[dict]):
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]
    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = 0
    process.wait = AsyncMock()
    process.pid = 12345
    return process


async def _drain_stream(adapter, **kwargs):
    async for _ in adapter.stream(cwd="/tmp", prompt="test", **kwargs):
        pass


class TestDisallowedTools:
    async def test_maps_canonical_denies_to_native_flag(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=["bash", "edit", "read"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--disallowed-tools" in cmd_args
        native = cmd_args[cmd_args.index("--disallowed-tools") + 1]
        # Deny id is run_terminal_cmd (not init.tools' run_terminal_command).
        assert "run_terminal_cmd" in native
        assert "run_terminal_command" not in native
        assert "search_replace" in native
        assert "write" in native
        assert "read_file" in native

    async def test_no_warning_when_all_canonical_denies_supported(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(
                    adapter,
                    disallowed_tools=["bash", "edit", "read", "web_search", "web_fetch"],
                )

        # Assert
        assert not any("cannot deny" in str(w.message) for w in recorded)

    async def test_no_disallowed_flag_when_none(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=None)

        # Assert
        assert "--disallowed-tools" not in mock_exec.call_args[0]


class TestAllowedToolsAndEffort:
    async def test_passes_allowed_tools_and_effort_flags(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(
                adapter,
                allowed_tools=["read_file", "list_dir"],
                effort="high",
            )

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--tools") + 1] == "read_file,list_dir"
        assert cmd_args[cmd_args.index("--reasoning-effort") + 1] == "high"

    async def test_uses_streaming_messages_json_output_format(self):
        # Arrange — full blocks, not streaming-json token deltas (issue #6 class).
        adapter = GrokAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter)

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--output-format") + 1] == "streaming-messages-json"
