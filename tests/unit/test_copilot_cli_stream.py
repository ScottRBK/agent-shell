import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.copilot_fixtures import (
    TURN_START_EVENT,
    MESSAGE_DELTA_EVENT,
    MESSAGE_EVENT_NO_TOOLS,
    RESULT_EVENT_SUCCESS,
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
        adapter = CopilotCLIAdapter()
        ndjson = [TURN_START_EVENT, MESSAGE_DELTA_EVENT, MESSAGE_EVENT_NO_TOOLS, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 3
        assert events[0].type == "system"
        assert events[1].type == "text"
        assert events[1].content == "HEL"
        assert events[2].type == "result"

    async def test_yields_error_event_on_nonzero_exit_with_stderr(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT]
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
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", model="gpt-4o"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--model" in cmd_args
        assert cmd_args[cmd_args.index("--model") + 1] == "gpt-4o"

    async def test_omits_model_flag_when_none(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--model" not in cmd_args

    async def test_includes_effort_flag_in_command(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
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
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--effort" not in cmd_args

    async def test_includes_allow_all_tools_by_default(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", auto_approve=True):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" in cmd_args

    async def test_omits_allow_all_tools_when_disabled(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" not in cmd_args

    async def test_includes_allow_tool_for_each_allowed_tool(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", allowed_tools=["Bash", "Read"]
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        allow_tool_indices = [i for i, x in enumerate(cmd_args) if x == "--allow-tool"]
        assert len(allow_tool_indices) == 2
        assert cmd_args[allow_tool_indices[0] + 1] == "Bash"
        assert cmd_args[allow_tool_indices[1] + 1] == "Read"

    async def test_omits_allow_tool_when_none(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", allowed_tools=None):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-tool" not in cmd_args

    async def test_includes_resume_flag_when_session_id_provided(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", session_id="ses_abc123"
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume" in cmd_args
        assert cmd_args[cmd_args.index("--resume") + 1] == "ses_abc123"

    async def test_omits_resume_flag_when_no_session_id(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume" not in cmd_args

    async def test_skips_malformed_json_lines(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        raw = json.dumps(MESSAGE_DELTA_EVENT) + "\n" + "not valid json\n" + json.dumps(RESULT_EVENT_SUCCESS) + "\n"
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

    async def test_base_command_is_copilot_with_flags(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="say hello"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "copilot"
        assert cmd_args[1] == "-p"
        assert cmd_args[2] == "say hello"
        assert "--output-format" in cmd_args
        assert cmd_args[cmd_args.index("--output-format") + 1] == "json"
        assert "--silent" in cmd_args
