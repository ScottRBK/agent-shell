import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.fixtures import (
    TEXT_EVENT,
    TOOL_USE_EVENT,
    RESULT_EVENT_SUCCESS,
    SYSTEM_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0, stderr: bytes = b""):
    """Create a mock subprocess that yields NDJSON lines from stdout."""
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]  # data then EOF

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = 12345
    return process


class TestStream:
    async def test_yields_events_in_order(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, TOOL_USE_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 3  # system event is ignored
        assert events[0].type == "text"
        assert events[1].type == "tool_use"
        assert events[2].type == "result"

    async def test_yields_error_event_on_nonzero_exit_with_stderr(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        ndjson = [TEXT_EVENT]
        mock_process = _make_mock_process(ndjson, returncode=1, stderr=b"something went wrong")

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert events[-1].type == "error"
        assert "something went wrong" in events[-1].content

    async def test_includes_effort_flag_in_command(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        ndjson = [TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", effort="high"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--effort" in cmd_args
        assert cmd_args[cmd_args.index("--effort") + 1] == "high"

    async def test_omits_effort_flag_when_none(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        ndjson = [TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--effort" not in cmd_args

    async def test_skips_malformed_json_lines(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        raw = json.dumps(TEXT_EVENT) + "\n" + "not valid json\n" + json.dumps(RESULT_EVENT_SUCCESS) + "\n"
        chunks = [raw.encode("utf-8"), b""]

        mock_process = AsyncMock()
        mock_process.stdout = MagicMock()
        mock_process.stdout.read = AsyncMock(side_effect=chunks)
        mock_process.stderr = MagicMock()
        mock_process.stderr.read = AsyncMock(return_value=b"")
        mock_process.returncode = 0
        mock_process.wait = AsyncMock()
        mock_process.pid = 12345

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 2
        assert events[0].type == "text"
        assert events[1].type == "result"
