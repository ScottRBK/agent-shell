import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

from tests.unit.cursor_fixtures import (
    SESSION_ID,
    SYSTEM_INIT_EVENT,
    USER_EVENT,
    THINKING_DELTA_EVENT,
    ASSISTANT_TEXT_EVENT,
    TOOL_CALL_SHELL_STARTED_EVENT,
    TOOL_CALL_SHELL_COMPLETED_EVENT,
    TOOL_CALL_MCP_STARTED_EVENT,
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
        shell = AgentShell(agent_type=AgentType.CURSOR)
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
        assert result_events[0].output_tokens == 46

    async def test_stream_with_shell_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        ndjson = [SYSTEM_INIT_EVENT, TOOL_CALL_SHELL_STARTED_EVENT,
                  TOOL_CALL_SHELL_COMPLETED_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="use shell"):
                events.append(event)

        # Assert — one tool_use (on start only), carrying the command.
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "echo hello-from-shell"

    async def test_stream_with_mcp_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        ndjson = [SYSTEM_INIT_EVENT, TOOL_CALL_MCP_STARTED_EVENT, ASSISTANT_TEXT_EVENT,
                  RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="use mcp"):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "plugin-serena-serena-write_memory"

    async def test_thinking_surfaced_only_when_requested(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        ndjson = [SYSTEM_INIT_EVENT, THINKING_DELTA_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]

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
    async def test_base_command_is_print_stream_json_trust(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "cursor-agent"
        assert "--print" in cmd_args
        assert cmd_args[cmd_args.index("--output-format") + 1] == "stream-json"
        assert "--trust" in cmd_args

    async def test_prompt_is_last_argument(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="say hello"):
                pass

        # Assert
        assert mock_exec.call_args[0][-1] == "say hello"

    async def test_includes_model_flag(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", model="sonnet-4-thinking"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--model") + 1] == "sonnet-4-thinking"

    async def test_omits_model_flag_when_absent(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert "--model" not in mock_exec.call_args[0]


class TestTrustAndForceIntegration:
    async def test_trust_always_present(self):
        # Arrange — --trust is mandatory in untrusted dirs; it is always passed, regardless
        # of auto_approve (it permits the run itself, not tool execution).
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act / Assert — both auto_approve settings carry --trust.
        with patch("asyncio.create_subprocess_exec",
                   return_value=_make_mock_process([RESULT_SUCCESS_EVENT])) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", auto_approve=True):
                pass
        assert "--trust" in mock_exec.call_args[0]

        with patch("asyncio.create_subprocess_exec",
                   return_value=_make_mock_process([RESULT_SUCCESS_EVENT])) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", auto_approve=False):
                pass
        assert "--trust" in mock_exec.call_args[0]

    async def test_force_present_by_default(self):
        # Arrange — auto_approve defaults True -> --force (auto-run tools).
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert "--force" in mock_exec.call_args[0]

    async def test_no_force_when_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--force" not in cmd_args
        assert "--trust" in cmd_args


class TestExecuteIntegration:
    async def test_execute_returns_response(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(cwd="/tmp", prompt="Reply PONG")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0
        assert response.output_tokens == 46
        assert response.duration == 2.964
        assert response.session_id == SESSION_ID


class TestErrorHandling:
    async def test_result_is_error_yields_error_result(self):
        # Arrange — a JSON result with is_error=True maps to an "error" status result event.
        shell = AgentShell(agent_type=AgentType.CURSOR)
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
        # Arrange — Cursor writes failures (e.g. ActionRequiredError) to stderr as plain text
        # with a non-zero exit and no JSON result event; the transport surfaces that.
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process(
            [SYSTEM_INIT_EVENT], returncode=1,
            stderr=b"ActionRequiredError: Named models unavailable",
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "ActionRequiredError" in error_events[0].content

    async def test_no_error_event_on_zero_exit_with_stderr(self):
        # Arrange — non-fatal warnings on stderr with a clean exit must not become errors.
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process(
            [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT],
            returncode=0, stderr=b"warning: something noisy",
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
        shell = AgentShell(agent_type=AgentType.CURSOR)
        good = (
            json.dumps(SYSTEM_INIT_EVENT) + "\n"
            + "this is not json\n"
            + json.dumps(ASSISTANT_TEXT_EVENT) + "\n"
            + json.dumps(RESULT_SUCCESS_EVENT) + "\n"
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
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT])

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="t"):
                events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert session_events[0].session_id == SESSION_ID

    async def test_passes_resume_flag_when_session_id_provided(self):
        # Arrange — the '=' form binds the id unambiguously ahead of the positional prompt.
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t", session_id="abc-123"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume=abc-123" in cmd_args

    async def test_omits_resume_flag_when_absent(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="t"):
                pass

        # Assert
        assert not any(str(a).startswith("--resume") for a in mock_exec.call_args[0])


class TestDisallowedToolsIntegration:
    async def test_deny_warns_and_adds_no_flag_through_shell(self):
        # Arrange — Cursor cannot enforce any per-call deny; the deny must warn and inject
        # no flag, even when routed through the AgentShell facade.
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="disallowed_tools"):
                async for _ in shell.stream(cwd="/tmp", prompt="t", disallowed_tools=["bash"]):
                    pass

        cmd_args = mock_exec.call_args[0]
        assert "--exclude-tools" not in cmd_args
