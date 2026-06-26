import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent

from tests.unit.codex_fixtures import (
    THREAD_STARTED_EVENT,
    TURN_STARTED_EVENT,
    AGENT_MESSAGE_COMPLETED_EVENT,
    AGENT_MESSAGE_COMPLETED_LONG_EVENT,
    AGENT_MESSAGE_COMPLETED_SECOND_EVENT,
    COMMAND_EXECUTION_STARTED_EVENT,
    COMMAND_EXECUTION_COMPLETED_EVENT,
    TURN_COMPLETED_EVENT,
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
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [
            THREAD_STARTED_EVENT,
            TURN_STARTED_EVENT,
            AGENT_MESSAGE_COMPLETED_LONG_EVENT,
            TURN_COMPLETED_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Count from 1 to 5.",
            ):
                events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert text_events[0].content.startswith("one calm")
        assert len(result_events) == 1, "Expected exactly one result event"


    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [
            THREAD_STARTED_EVENT,
            TURN_STARTED_EVENT,
            AGENT_MESSAGE_COMPLETED_EVENT,
            COMMAND_EXECUTION_STARTED_EVENT,
            COMMAND_EXECUTION_COMPLETED_EVENT,
            AGENT_MESSAGE_COMPLETED_SECOND_EVENT,
            TURN_COMPLETED_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(
                cwd="/tmp",
                prompt="Use bash to echo hi-from-tool-call",
            ):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        text_events = [e for e in events if e.type == "text"]
        assert len(tool_events) == 1, "Expected exactly one tool_use (only on completed item)"
        assert "echo hi-from-tool-call" in tool_events[0].content
        assert len(text_events) == 2, "Expected two text events"


class TestCommandConstructionIntegration:
    async def test_base_command_includes_codex_exec_json_and_skip_git(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "codex"
        assert cmd_args[1] == "exec"
        assert "--json" in cmd_args
        assert "--skip-git-repo-check" in cmd_args

    async def test_includes_sandbox_when_no_session(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--sandbox" in cmd_args

    async def test_prompt_is_last_argument(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="say hello"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[-1] == "say hello"

    async def test_includes_model_flag(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test", model="gpt-5.4-mini"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--model" in cmd_args
        assert cmd_args[cmd_args.index("--model") + 1] == "gpt-5.4-mini"

    async def test_includes_effort_via_config_override(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test", effort="high"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-c" in cmd_args
        idx = cmd_args.index("-c")
        assert cmd_args[idx + 1] == 'model_reasoning_effort="high"'

    async def test_omits_effort_override_when_none(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-c" not in cmd_args


class TestAutoApproveIntegration:
    async def test_includes_bypass_flag_when_auto_approve_default(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd_args

    async def test_omits_bypass_flag_when_auto_approve_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd_args


class TestExecuteIntegration:
    async def test_execute_returns_response_with_text(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [
            THREAD_STARTED_EVENT,
            TURN_STARTED_EVENT,
            AGENT_MESSAGE_COMPLETED_EVENT,
            TURN_COMPLETED_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(
                cwd="/tmp",
                prompt="Reply with PONG.",
            )

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0
        assert response.output_tokens == 22, (
            "Expected raw output_tokens (reasoning-inclusive) from turn.completed.usage"
        )


class TestStderrAndErrorEvents:
    async def test_stderr_emits_error_event_on_nonzero_exit(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(
            ndjson, returncode=1, stderr=b"codex auth failed: please run codex login"
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "auth failed" in error_events[0].content

    async def test_stderr_does_not_emit_error_on_zero_exit(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(
            ndjson, returncode=0, stderr=b"some warning on stderr"
        )

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        error_events = [e for e in events if e.type == "error"]
        assert error_events == []


class TestMalformedJsonTolerance:
    async def test_skips_malformed_json_line_and_continues(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        good_lines = (
            json.dumps(THREAD_STARTED_EVENT) + "\n"
            + "this is not json\n"
            + json.dumps(AGENT_MESSAGE_COMPLETED_EVENT) + "\n"
            + json.dumps(TURN_COMPLETED_EVENT) + "\n"
        )
        chunks = [good_lines.encode("utf-8"), b""]
        process = AsyncMock()
        process.stdout = MagicMock()
        process.stdout.read = AsyncMock(side_effect=chunks)
        process.stderr = MagicMock()
        process.stderr.read = AsyncMock(return_value=b"")
        process.returncode = 0
        process.wait = AsyncMock()
        process.pid = 99999

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for event in shell.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert — malformed line was skipped, good events came through
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]
        assert len(text_events) == 1
        assert text_events[0].content == "PONG"
        assert len(result_events) == 1


class TestSessionIntegration:
    async def test_stream_captures_session_id_from_thread_started(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [
            THREAD_STARTED_EVENT,
            TURN_STARTED_EVENT,
            AGENT_MESSAGE_COMPLETED_EVENT,
            TURN_COMPLETED_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in shell.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert len(session_events) >= 1
        assert session_events[0].session_id == "019e115b-8594-7393-8ed4-bd6cf6127f2a"

    async def test_execute_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [
            THREAD_STARTED_EVENT,
            TURN_STARTED_EVENT,
            AGENT_MESSAGE_COMPLETED_EVENT,
            TURN_COMPLETED_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await shell.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.session_id == "019e115b-8594-7393-8ed4-bd6cf6127f2a"

    async def test_stream_uses_resume_subcommand_when_session_id_provided(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp", prompt="test", session_id="abc-123"
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "codex"
        assert cmd_args[1] == "exec"
        assert cmd_args[2] == "resume"
        assert "abc-123" in cmd_args
        # Resume must NOT include sandbox or bypass flags (codex rejects them)
        assert "--sandbox" not in cmd_args
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd_args

    async def test_stream_omits_resume_when_no_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "resume" not in cmd_args


class TestDisallowedToolsIntegration:
    async def test_web_search_deny_reaches_command_through_shell(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        ndjson = [
            THREAD_STARTED_EVENT,
            TURN_STARTED_EVENT,
            AGENT_MESSAGE_COMPLETED_EVENT,
            TURN_COMPLETED_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in shell.stream(
                cwd="/tmp", prompt="test", disallowed_tools=["web_search"]
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert 'web_search="disabled"' in cmd_args
