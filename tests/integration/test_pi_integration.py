import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

from tests.unit.pi_fixtures import (
    SESSION_EVENT,
    AGENT_START_EVENT,
    TURN_START_EVENT,
    TEXT_END_UPDATE,
    THINKING_END_UPDATE,
    TOOL_EXECUTION_START_EVENT,
    TOOL_EXECUTION_END_EVENT,
    AGENT_END_TEXT_EVENT,
    AGENT_END_TOOLUSE_EVENT,
    AGENT_END_ERROR_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0, stderr: bytes = b""):
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
        shell = AgentShell(agent_type=AgentType.PI)
        ndjson = [SESSION_EVENT, AGENT_START_EVENT, TURN_START_EVENT,
                  TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="Reply PONG"):
                events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]
        assert [e.content for e in text_events] == ["PONG"]
        assert len(result_events) == 1
        assert result_events[0].content == "ok"
        assert result_events[0].output_tokens == 27

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        ndjson = [SESSION_EVENT, TOOL_EXECUTION_START_EVENT, TOOL_EXECUTION_END_EVENT,
                  TEXT_END_UPDATE, AGENT_END_TOOLUSE_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="use bash"):
                events.append(event)

        # Assert — one tool_use (on start only), result tokens summed across turns.
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "bash"
        result = [e for e in events if e.type == "result"][0]
        assert result.output_tokens == 55

    async def test_thinking_surfaced_only_when_requested(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        ndjson = [SESSION_EVENT, THINKING_END_UPDATE, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]

        # Act — include_thinking=False
        with patch("asyncio.create_subprocess_exec", return_value=_make_mock_process(ndjson)):
            off = [e async for e in shell.stream(cwd="/tmp", prompt="x", include_thinking=False)]
        # Act — include_thinking=True
        with patch("asyncio.create_subprocess_exec", return_value=_make_mock_process(ndjson)):
            on = [e async for e in shell.stream(cwd="/tmp", prompt="x", include_thinking=True)]

        # Assert
        assert [e for e in off if e.type == "thinking"] == []
        assert len([e for e in on if e.type == "thinking"]) == 1


class TestCommandConstructionIntegration:
    async def test_base_command_is_pi_mode_json_print(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "pi"
        assert cmd_args[1] == "--mode"
        assert cmd_args[2] == "json"
        assert "--print" in cmd_args

    async def test_prompt_is_last_argument(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="say hello"):
                pass

        # Assert
        assert mock_exec.call_args[0][-1] == "say hello"

    async def test_includes_model_flag(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", model="anthropic/claude-opus-4-8"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--model") + 1] == "anthropic/claude-opus-4-8"

    async def test_effort_maps_to_thinking(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", effort="medium"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--thinking") + 1] == "medium"

    async def test_omits_thinking_when_effort_none(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert "--thinking" not in mock_exec.call_args[0]


class TestAutoApproveIntegration:
    async def test_approve_flag_present_by_default(self):
        # Arrange — neither flag would hang pi on a trust prompt, so one is always passed.
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--approve" in cmd_args
        assert "--no-approve" not in cmd_args

    async def test_no_approve_flag_when_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--no-approve" in cmd_args
        assert "--approve" not in cmd_args


class TestExecuteIntegration:
    async def test_execute_returns_response(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        ndjson = [SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(cwd="/tmp", prompt="Reply PONG")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0
        assert response.output_tokens == 27
        assert response.session_id == "019f0ae6-995e-780b-b2e7-f00d2d72873f"


class TestErrorHandling:
    async def test_agent_end_error_yields_error_result(self):
        # Arrange — pi exits 0 on a model error; status comes from agent_end stopReason.
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([SESSION_EVENT, AGENT_END_ERROR_EVENT])

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        result = [e for e in events if e.type == "result"][0]
        assert result.content == "error"

    async def test_stderr_emits_error_event_on_nonzero_exit(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process(
            [AGENT_END_TEXT_EVENT], returncode=1, stderr=b"pi: fatal: provider unreachable"
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "provider unreachable" in error_events[0].content

    async def test_no_error_event_on_zero_exit_with_stderr(self):
        # Arrange — pi writes non-fatal warnings to stderr even on success.
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process(
            [AGENT_END_TEXT_EVENT], returncode=0, stderr=b"Warning: using custom model id"
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        assert [e for e in events if e.type == "error"] == []


class TestMalformedJsonTolerance:
    async def test_skips_malformed_line_and_continues(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        good = (
            json.dumps(SESSION_EVENT) + "\n"
            + "this is not json\n"
            + json.dumps(TEXT_END_UPDATE) + "\n"
            + json.dumps(AGENT_END_TEXT_EVENT) + "\n"
        )
        process = AsyncMock()
        process.stdout = MagicMock()
        process.stdout.read = AsyncMock(side_effect=[good.encode("utf-8"), b""])
        process.stderr = MagicMock()
        process.stderr.read = AsyncMock(return_value=b"")
        process.returncode = 0
        process.wait = AsyncMock()
        process.pid = 99999

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        assert [e.content for e in events if e.type == "text"] == ["PONG"]
        assert len([e for e in events if e.type == "result"]) == 1


class TestSessionIntegration:
    async def test_captures_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT])

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert session_events[0].session_id == "019f0ae6-995e-780b-b2e7-f00d2d72873f"

    async def test_passes_session_id_flag_when_provided(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", session_id="abc-123"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--session-id") + 1] == "abc-123"

    async def test_omits_session_id_flag_when_absent(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert "--session-id" not in mock_exec.call_args[0]


class TestDisallowedToolsIntegration:
    async def test_deny_reaches_command_through_shell(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", disallowed_tools=["bash", "edit"]):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--exclude-tools") + 1] == "bash,edit,write"
