import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

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


class TestStreamIntegration:
    async def test_stream_yields_text_and_result_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Respond with exactly: hello world",
            ):
                events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
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
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Use bash to list files",
            ):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected at least one tool_use event"
        assert tool_events[0].content == "bash"


class TestCommandConstructionIntegration:
    async def test_includes_model_flag(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp",
                prompt="test",
                model="anthropic/claude-sonnet-4-5",
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-m" in cmd_args
        assert cmd_args[cmd_args.index("-m") + 1] == "anthropic/claude-sonnet-4-5"

    async def test_prompt_is_last_argument(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="do something"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[-1] == "do something"

    async def test_base_command_includes_run_and_format_json(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "opencode"
        assert cmd_args[1] == "run"
        assert "--format" in cmd_args
        assert cmd_args[cmd_args.index("--format") + 1] == "json"


class TestExecuteIntegration:
    async def test_execute_returns_response_with_text_and_cost(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(
                cwd="/tmp",
                prompt="Respond with exactly: hello world",
            )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost == 0.05


class TestSessionIntegration:
    async def test_stream_captures_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert len(session_events) >= 1, "Expected at least one event with session_id"
        assert session_events[0].session_id == "test-session"

    async def test_stream_passes_session_flag_when_session_id_provided(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp", prompt="test", session_id="ses_abc123"
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-s" in cmd_args
        assert cmd_args[cmd_args.index("-s") + 1] == "ses_abc123"

    async def test_stream_omits_session_flag_when_no_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-s" not in cmd_args

    async def test_execute_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(cwd="/tmp", prompt="test")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.session_id == "test-session"
