import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

from tests.unit.fixtures import (
    SYSTEM_EVENT,
    TEXT_EVENT,
    THINKING_EVENT,
    TOOL_USE_EVENT,
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


class TestStreamIntegration:
    async def test_stream_yields_text_and_result_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Respond with exactly: hello world",
                allowed_tools=[],
            ):
                events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"
        assert result_events[0].cost > 0, "Expected cost to be greater than 0"
        assert result_events[0].duration > 0, "Expected duration to be greater than 0"

    async def test_stream_with_thinking_yields_thinking_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, THINKING_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Respond with exactly: hello world",
                allowed_tools=[],
                effort="high",
                include_thinking=True,
            ):
                events.append(event)

        # Assert
        thinking_events = [e for e in events if e.type == "thinking"]
        assert len(thinking_events) >= 1, "Expected at least one thinking event"

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, TOOL_USE_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="List the files in the current directory using the Bash tool",
                allowed_tools=["Bash"],
            ):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected at least one tool_use event"


class TestAutoApproveIntegration:
    async def test_stream_includes_skip_permissions_by_default(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--dangerously-skip-permissions" in cmd_args

    async def test_stream_omits_skip_permissions_when_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--dangerously-skip-permissions" not in cmd_args

    async def test_execute_includes_skip_permissions_by_default(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await shell.execute(cwd="/tmp", prompt="test")

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--dangerously-skip-permissions" in cmd_args


class TestExecuteIntegration:
    async def test_execute_returns_response_with_text_and_cost(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(
                cwd="/tmp",
                prompt="Respond with exactly: hello world",
                allowed_tools=[],
            )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost > 0, "Expected cost to be greater than 0"

    async def test_execute_with_effort(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        ndjson = [SYSTEM_EVENT, THINKING_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            response = await shell.execute(
                cwd="/tmp",
                prompt="Respond with exactly: hello world",
                allowed_tools=[],
                effort="high",
            )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost > 0, "Expected cost to be greater than 0"
        cmd_args = mock_exec.call_args[0]
        assert "--effort" in cmd_args
        assert cmd_args[cmd_args.index("--effort") + 1] == "high"
