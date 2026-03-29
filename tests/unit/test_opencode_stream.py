import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT,
    TOOL_USE_EVENT,
    STEP_FINISH_STOP_EVENT,
    STEP_FINISH_TOOL_CALLS_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0, stderr: bytes = b""):
    """Create a mock subprocess that yields NDJSON lines from stdout."""
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]

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
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 3
        assert events[0].type == "system"
        assert events[0].session_id == "test-session"
        assert events[1].type == "text"
        assert events[2].type == "result"

    async def test_yields_tool_use_events(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [
            STEP_START_EVENT,
            TOOL_USE_EVENT,
            STEP_FINISH_TOOL_CALLS_EVENT,
            STEP_START_EVENT,
            TEXT_EVENT,
            STEP_FINISH_STOP_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "bash"

    async def test_yields_error_event_on_nonzero_exit_with_stderr(self):
        # Arrange
        adapter = OpenCodeAdapter()
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

    async def test_includes_model_flag_in_command(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", model="anthropic/claude-sonnet-4-5"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-m" in cmd_args
        assert cmd_args[cmd_args.index("-m") + 1] == "anthropic/claude-sonnet-4-5"

    async def test_omits_model_flag_when_none(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-m" not in cmd_args

    async def test_includes_session_flag_in_command(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", session_id="ses_abc123"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-s" in cmd_args
        assert cmd_args[cmd_args.index("-s") + 1] == "ses_abc123"

    async def test_omits_session_flag_when_none(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-s" not in cmd_args

    async def test_prompt_is_last_argument(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="do something"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[-1] == "do something"

    async def test_skips_malformed_json_lines(self):
        # Arrange
        adapter = OpenCodeAdapter()
        raw = json.dumps(TEXT_EVENT) + "\n" + "not valid json\n" + json.dumps(STEP_FINISH_STOP_EVENT) + "\n"
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
