import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

from tests.unit.copilot_fixtures import (
    TURN_START_EVENT,
    MESSAGE_DELTA_EVENT,
    MESSAGE_DELTA_EVENT_2,
    MESSAGE_DELTA_EVENT_3,
    REASONING_DELTA_EVENT,
    MESSAGE_EVENT_NO_TOOLS,
    MESSAGE_EVENT_WITH_TOOLS,
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
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [
            TURN_START_EVENT,
            MESSAGE_DELTA_EVENT,
            MESSAGE_EVENT_NO_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
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

    async def test_stream_with_thinking_yields_thinking_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [
            TURN_START_EVENT,
            REASONING_DELTA_EVENT,
            MESSAGE_DELTA_EVENT,
            MESSAGE_EVENT_NO_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
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
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [
            TURN_START_EVENT,
            MESSAGE_DELTA_EVENT,
            MESSAGE_EVENT_WITH_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Use bash to list files",
                allowed_tools=["Bash"],
            ):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected at least one tool_use event"
        assert tool_events[0].content == "report_intent"
        assert tool_events[1].content == "bash"


class TestAutoApproveIntegration:
    async def test_stream_includes_allow_all_tools_by_default(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" in cmd_args

    async def test_stream_omits_allow_all_tools_when_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" not in cmd_args

    async def test_execute_includes_allow_all_tools_by_default(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await shell.execute(cwd="/tmp", prompt="test")

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" in cmd_args


class TestCommandConstructionIntegration:
    async def test_base_command_includes_copilot_and_flags(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "copilot"
        assert cmd_args[1] == "-p"
        assert "--output-format" in cmd_args
        assert cmd_args[cmd_args.index("--output-format") + 1] == "json"
        assert "--silent" in cmd_args

    async def test_includes_model_flag(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp",
                prompt="test",
                model="gpt-4o",
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--model" in cmd_args
        assert cmd_args[cmd_args.index("--model") + 1] == "gpt-4o"

    async def test_includes_effort_flag(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp",
                prompt="test",
                effort="high",
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--effort" in cmd_args
        assert cmd_args[cmd_args.index("--effort") + 1] == "high"

    async def test_prompt_is_second_argument(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="say hello"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[2] == "say hello"

    async def test_stream_includes_reasoning_flag_when_include_thinking(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp",
                prompt="test",
                include_thinking=True,
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--enable-reasoning-summaries" in cmd_args

    async def test_stream_omits_reasoning_flag_when_not_include_thinking(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp",
                prompt="test",
                include_thinking=False,
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--enable-reasoning-summaries" not in cmd_args


class TestExecuteIntegration:
    async def test_execute_returns_response_with_text(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [
            TURN_START_EVENT,
            MESSAGE_DELTA_EVENT,
            MESSAGE_EVENT_NO_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
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
        assert response.cost == 0.0, "Expected cost to be 0.0 (Copilot has no pricing)"


class TestSessionIntegration:
    async def test_stream_captures_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, MESSAGE_EVENT_NO_TOOLS, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert len(session_events) >= 1, "Expected at least one event with session_id"
        assert session_events[0].session_id == "01036873-9931-4e3e-b3cb-14793ae370f9"

    async def test_stream_passes_resume_flag_when_session_id_provided(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp", prompt="test", session_id="abc-123"
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume" in cmd_args
        assert cmd_args[cmd_args.index("--resume") + 1] == "abc-123"

    async def test_stream_omits_resume_flag_when_no_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume" not in cmd_args

    async def test_execute_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        ndjson = [MESSAGE_DELTA_EVENT, MESSAGE_EVENT_NO_TOOLS, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(cwd="/tmp", prompt="test")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.session_id == "01036873-9931-4e3e-b3cb-14793ae370f9"
