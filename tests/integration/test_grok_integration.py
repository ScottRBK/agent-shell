import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

from tests.unit.grok_fixtures import (
    SESSION_ID,
    SYSTEM_INIT_EVENT,
    USER_EVENT,
    ASSISTANT_TEXT_EVENT,
    ASSISTANT_THINKING_AND_TEXT_EVENT,
    ASSISTANT_TOOL_USE_EVENT,
    RESULT_SUCCESS_EVENT,
    RESULT_ERROR_EVENT,
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
        shell = AgentShell(agent_type=AgentType.GROK)
        ndjson = [SYSTEM_INIT_EVENT, USER_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
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
        assert result_events[0].cost == 0.0359528

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        ndjson = [
            SYSTEM_INIT_EVENT,
            ASSISTANT_TOOL_USE_EVENT,
            ASSISTANT_TEXT_EVENT,
            RESULT_SUCCESS_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="use list_dir"):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "list_dir"

    async def test_thinking_surfaced_only_when_requested(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_THINKING_AND_TEXT_EVENT, RESULT_SUCCESS_EVENT]

        # Act
        with patch("asyncio.create_subprocess_exec",
                   return_value=_make_mock_process(ndjson)):
            off = [
                e async for e in shell.stream(
                    cwd="/tmp", prompt="x", include_thinking=False,
                )
            ]
        with patch("asyncio.create_subprocess_exec",
                   return_value=_make_mock_process(ndjson)):
            on = [
                e async for e in shell.stream(
                    cwd="/tmp", prompt="x", include_thinking=True,
                )
            ]

        # Assert
        assert [e for e in off if e.type == "thinking"] == []
        assert len([e for e in on if e.type == "thinking"]) == 1


class TestCommandConstructionIntegration:
    async def test_base_command_is_print_streaming_messages_json(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "grok"
        assert cmd_args[1] == "-p"
        assert cmd_args[2] == "test"
        assert cmd_args[cmd_args.index("--output-format") + 1] == "streaming-messages-json"
        assert "--always-approve" in cmd_args

    async def test_includes_model_and_effort_flags(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp", prompt="t", model="grok-4.5", effort="high",
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("-m") + 1] == "grok-4.5"
        assert cmd_args[cmd_args.index("--reasoning-effort") + 1] == "high"

    async def test_omits_model_when_absent(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert "-m" not in mock_exec.call_args[0]

    async def test_no_always_approve_when_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", auto_approve=False):
                pass

        # Assert
        assert "--always-approve" not in mock_exec.call_args[0]


class TestExecuteIntegration:
    async def test_execute_returns_response(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(cwd="/tmp", prompt="Reply PONG")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0359528
        assert response.output_tokens == 27
        assert response.duration == 4.034
        assert response.session_id == SESSION_ID


class TestErrorHandling:
    async def test_result_is_error_yields_error_result(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([SYSTEM_INIT_EVENT, RESULT_ERROR_EVENT])

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
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process(
            [SYSTEM_INIT_EVENT], returncode=1,
            stderr=b"Not signed in. Run `grok login`.",
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "Not signed in" in error_events[0].content


class TestSessionIntegration:
    async def test_captures_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process(
            [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT],
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert session_events[0].session_id == SESSION_ID

    async def test_passes_resume_flag_when_session_id_provided(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", session_id="abc-123"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--resume") + 1] == "abc-123"

    async def test_omits_resume_flag_when_absent(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert "--resume" not in mock_exec.call_args[0]


class TestDisallowedToolsIntegration:
    async def test_deny_maps_through_shell(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp", prompt="t", disallowed_tools=["bash", "web_search"],
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        native = cmd_args[cmd_args.index("--disallowed-tools") + 1]
        assert "run_terminal_cmd" in native
        assert "web_search" in native
