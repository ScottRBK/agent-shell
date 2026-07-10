"""Health-check integration tests.

These drive `AgentShell.health_check` through the real adapter chain with a mocked
subprocess emitting each agent's *actual captured* NDJSON. They validate the one
cross-adapter rule the CLI probes established:

    healthy  <=>  a `result` event with content == "ok" arrives and no `error` event.

The failure cases deliberately reproduce the real CLI quirks: opencode exits 0 on
an unhealthy run (so exit code alone is not a signal), and claude/copilot surface a
bad model as a `result` whose status is "error".
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, HealthCheckResult

from tests.unit import fixtures as claude_fx
from tests.unit import opencode_fixtures as oc_fx
from tests.unit import codex_fixtures as cx_fx
from tests.unit import copilot_fixtures as cp_fx
from tests.unit import pi_fixtures as pi_fx
from tests.unit import cursor_fixtures as cursor_fx


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


# A successful turn per adapter: the minimal event sequence that ends in a
# normalized `result` event with content == "ok".
HAPPY = {
    AgentType.CLAUDE_CODE: [claude_fx.SYSTEM_EVENT, claude_fx.TEXT_EVENT,
                            claude_fx.RESULT_EVENT_SUCCESS],
    AgentType.OPENCODE: [oc_fx.STEP_START_EVENT, oc_fx.TEXT_EVENT,
                         oc_fx.STEP_FINISH_STOP_EVENT],
    AgentType.CODEX: [cx_fx.THREAD_STARTED_EVENT, cx_fx.AGENT_MESSAGE_COMPLETED_EVENT,
                      cx_fx.TURN_COMPLETED_EVENT],
    AgentType.COPILOT_CLI: [cp_fx.MESSAGE_EVENT_NO_TOOLS, cp_fx.RESULT_EVENT_SUCCESS],
    AgentType.PI: [pi_fx.SESSION_EVENT, pi_fx.TEXT_END_UPDATE, pi_fx.AGENT_END_TEXT_EVENT],
    AgentType.CURSOR: [cursor_fx.SYSTEM_INIT_EVENT, cursor_fx.ASSISTANT_TEXT_EVENT,
                       cursor_fx.RESULT_SUCCESS_EVENT],
}


class TestHealthyCombinations:
    @pytest.mark.parametrize("agent_type", list(HAPPY.keys()))
    async def test_healthy_when_result_ok(self, agent_type):
        # Arrange
        shell = AgentShell(agent_type=agent_type)
        mock_process = _make_mock_process(HAPPY[agent_type])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="some-model")

        # Assert
        assert isinstance(result, HealthCheckResult)
        assert result.healthy is True
        assert result.exception is None


class TestUnhealthyCombinations:
    async def test_claude_result_error_is_unhealthy(self):
        # Arrange — claude exits 1 on a bad model; status comes from is_error in the result.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        mock_process = _make_mock_process(
            [claude_fx.SYSTEM_EVENT, claude_fx.RESULT_EVENT_ERROR], returncode=1
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus-model")

        # Assert
        assert result.healthy is False
        assert result.exception is not None

    async def test_copilot_result_error_is_unhealthy(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        mock_process = _make_mock_process([cp_fx.RESULT_EVENT_ERROR], returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus-model")

        # Assert
        assert result.healthy is False
        assert result.exception is not None

    async def test_opencode_error_event_on_zero_exit_is_unhealthy(self):
        # Arrange — the critical case: opencode returns exit 0 even when the run failed
        # (bad model OR billing). Exit code alone would wrongly read as healthy.
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        mock_process = _make_mock_process([oc_fx.ERROR_EVENT], returncode=0)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus-model")

        # Assert
        assert result.healthy is False
        assert result.exception is not None

    async def test_codex_no_result_event_is_unhealthy(self):
        # Arrange — codex bad model: no turn.completed (=> no result), error on stderr.
        shell = AgentShell(agent_type=AgentType.CODEX)
        mock_process = _make_mock_process(
            [cx_fx.THREAD_STARTED_EVENT, cx_fx.TURN_STARTED_EVENT],
            returncode=1,
            stderr=b"The 'bogus-model' model is not supported",
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus-model")

        # Assert
        assert result.healthy is False
        assert result.exception is not None

    async def test_codex_turn_failed_surfaces_real_reason(self):
        # Arrange — codex puts the real failure on stdout (turn.failed) while stderr only
        # has the useless "Reading additional input from stdin...". The reason must come
        # from turn.failed, not the stderr noise.
        shell = AgentShell(agent_type=AgentType.CODEX)
        mock_process = _make_mock_process(
            [cx_fx.THREAD_STARTED_EVENT, cx_fx.TURN_STARTED_EVENT, cx_fx.TURN_FAILED_EVENT],
            returncode=1,
            stderr=b"Reading additional input from stdin...\n",
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus-model")

        # Assert
        assert result.healthy is False
        assert "not supported" in result.exception
        assert "stdin" not in result.exception

    async def test_pi_result_error_is_unhealthy(self):
        # Arrange — pi exits 0 on a runtime model error; status is in agent_end.
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process([pi_fx.SESSION_EVENT, pi_fx.AGENT_END_ERROR_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="some-model")

        # Assert
        assert result.healthy is False
        assert result.exception is not None

    async def test_pi_bad_model_name_stderr_is_unhealthy(self):
        # Arrange — unknown model name: pi exits 1 with the reason on stderr, empty stdout.
        shell = AgentShell(agent_type=AgentType.PI)
        mock_process = _make_mock_process(
            [], returncode=1, stderr=b'Error: Model "bogus" not found.'
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus")

        # Assert
        assert result.healthy is False
        assert "not found" in result.exception

    async def test_cursor_bad_model_name_stderr_is_unhealthy(self):
        # Arrange — unknown model: cursor-agent exits 1 with the reason on stderr and empty
        # stdout (no result event). Captured live; the real stderr is ~4.4KB — the reason
        # ("Cannot use this model: <name>") sits at the FRONT, followed by the full model
        # list. Unlike pi (short stderr), a tail-only truncation would drop Cursor's reason;
        # `format_stderr` keeps both ends so it survives.
        shell = AgentShell(agent_type=AgentType.CURSOR)
        mock_process = _make_mock_process(
            [],
            returncode=1,
            stderr=(
                b"Cannot use this model: bogus. Available models: auto, gpt-5.6-terra-high, "
                b"gpt-5.6-sol-high, gpt-5.6-luna-high, claude-opus-4-8-high, "
                b"claude-sonnet-5-high, claude-fable-5-high, gpt-5.5-high, gpt-5.4-high, "
                b"gpt-5.2-high, gpt-5.1-high, grok-4.5-high, composer-2.5, gemini-3.1-pro, "
                b"claude-4-sonnet, claude-opus-4-7-high, kimi-k2.7-code, glm-5.2-high, "
                b"gpt-5.4-mini-high, gpt-5.4-nano-high"
            ),
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await shell.health_check(cwd="/tmp", model="bogus")

        # Assert
        assert result.healthy is False
        assert "Cannot use this model: bogus" in result.exception


class TestHealthCheckValidation:
    async def test_rejects_missing_cwd(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

        # Act / Assert
        with pytest.raises(ValueError, match="Directory does not exist"):
            await shell.health_check(cwd="/does/not/exist", model="m")
