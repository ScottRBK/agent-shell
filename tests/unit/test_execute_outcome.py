"""Outcome contract for execute(): a failed run raises instead of returning (issue #11).

execute() collapses a whole stream into one AgentResponse, and the collector kept only the
text and the metrics — so every normalized failure was discarded and a failed run was
indistinguishable from a successful one that produced no text. There are exactly three
shapes a normalized failure can take:

  1. an `error` event (stderr on a non-zero exit; codex's turn.failed; opencode's
     stream-level error);
  2. a terminal `result` whose content is "error";
  3. no terminal `result` at all (a killed or truncated run).

Success is the same rule the health probe applies: a `result` event with content == "ok"
and no `error` event. The first group drives that from the public AgentShell.execute()
boundary with only the subprocess mocked, so the real adapter and the real collector run;
the last group then pins the identical contract on all adapters.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.models.agent import AgentExecutionError, AgentResponse, AgentType
from agent_shell.shell import AgentShell

from tests.unit.adapter_matrix import (
    ADAPTERS,
    ERROR_RESULT_ADAPTERS,
    ERROR_RESULT_EVENT,
    OK_RESULT_EVENT,
)
from tests.unit.fixtures import RESULT_EVENT_ERROR, RESULT_EVENT_SUCCESS, SYSTEM_EVENT, TEXT_EVENT
from tests.unit.opencode_fixtures import ERROR_EVENT_EMPTY_MESSAGE
from tests.unit.pi_fixtures import (
    AGENT_END_ERROR_EVENT,
    AGENT_END_RETRY_PENDING_EVENT,
    AGENT_END_RETRY_SUCCEEDED_EVENT,
    AGENT_END_TEXT_EVENT,
    SESSION_EVENT,
    TEXT_END_UPDATE,
)

PI_SESSION_ID = "019f0ae6-995e-780b-b2e7-f00d2d72873f"


def _make_mock_process(ndjson_lines: list[dict], stderr: bytes = b"", returncode: int = 0):
    encoded = "".join(json.dumps(line) + "\n" for line in ndjson_lines)
    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=[encoded.encode("utf-8"), b""])
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = 12345
    return process


class TestSuccessfulRun:
    async def test_returns_the_aggregated_response(self):
        # Arrange — guards the aggregation the raising contract must leave untouched.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        second_text = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "And some more text."}]},
        }
        process = _make_mock_process([SYSTEM_EVENT, TEXT_EVENT, second_text,
                                      RESULT_EVENT_SUCCESS])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            response = await shell.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "Hey! Here's some text output.\nAnd some more text."
        assert response.cost == 0.16098
        assert response.duration == 10.37
        assert response.output_tokens == 6
        assert response.session_id == "test-session"


class TestErrorEventFailure:
    """Shape 1 — an `error` event was emitted."""

    async def test_stderr_reason_reaches_the_caller(self):
        # Arrange — issue #11's own repro: the child fails with a real reason on stderr and
        # never reports a result, and execute() returned an empty success-shaped response.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        reason = 'Error: Model "definitely-not-a-real-agent-shell-model" not found.'
        process = _make_mock_process([SYSTEM_EVENT, TEXT_EVENT], stderr=reason.encode("utf-8"),
                                     returncode=1)

        # Act / Assert — the reason is the exception's own string, not a generic message.
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        assert str(excinfo.value) == reason
        assert excinfo.value.reason == reason

    async def test_partial_run_data_rides_on_the_exception(self):
        # Arrange — text and session id produced before the failure must survive the raise.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        process = _make_mock_process([SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS],
                                     stderr=b"provider unreachable", returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        # Assert — an `error` event fails the run even though an "ok" result arrived, and
        # everything the old return value carried is still reachable.
        error = excinfo.value
        assert str(error) == "provider unreachable"
        assert error.response == "Hey! Here's some text output."
        assert error.cost == 0.16098
        assert error.duration == 10.37
        assert error.output_tokens == 6
        assert error.session_id == "test-session"


class TestErrorResultFailure:
    """Shape 2 — the terminal `result` event reports content == "error"."""

    async def test_error_result_raises_the_generic_reason(self):
        # Arrange — nothing but "it failed" is recoverable from this harness.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        process = _make_mock_process([SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_ERROR])

        # Act / Assert — wording matches the health probe's, so both surfaces read alike.
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError, match="agent reported an error result"):
                await shell.execute(cwd="/tmp", prompt="ping")

    async def test_pi_error_message_reaches_the_exception_string(self):
        # Arrange — pi carries the real cause on the failing result (issue #10). End to end,
        # that reason must be what a consumer sees when it only logs the exception.
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_ERROR_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert str(excinfo.value) == "500 model name=qwen3.6-27b-8Q failed to load"
        assert excinfo.value.response == "PONG"
        assert excinfo.value.session_id == PI_SESSION_ID


class TestReasonPrecedence:
    """Which reason a caller is told when one failing run left more than one behind."""

    async def test_error_event_reason_beats_a_detail_less_error_result(self):
        # Arrange — the terminal result says only "error" while stderr carries the actual
        # cause. Reporting the generic wording here would discard the only useful detail.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        reason = "Cannot use this model: bogus."
        process = _make_mock_process([SYSTEM_EVENT, RESULT_EVENT_ERROR],
                                     stderr=reason.encode("utf-8"), returncode=1)

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        assert str(excinfo.value) == reason

    async def test_adapter_recovered_result_reason_beats_a_stderr_tail(self):
        # Arrange — the S2 scenario, and what real pi does: the failing agent_end carries the
        # actual cause while the child ALSO exits non-zero with unrelated node noise on stderr.
        # Every adapter turns that stderr into an `error` event, so ranking the error event
        # first would throw away issue #10's recovered reason on every non-zero pi exit.
        shell = AgentShell(agent_type=AgentType.PI)
        noise = "node:internal/process/promises warning: unhandled rejection"
        process = _make_mock_process([SESSION_EVENT, AGENT_END_ERROR_EVENT],
                                     stderr=noise.encode("utf-8"), returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert str(excinfo.value) == "500 model name=qwen3.6-27b-8Q failed to load"

    async def test_health_check_agrees_when_both_kinds_carry_detail(self):
        # Arrange — the same stream as above through the other surface. outcome.py exists so
        # these two can never word one failure differently.
        shell = AgentShell(agent_type=AgentType.PI)
        noise = "node:internal/process/promises warning: unhandled rejection"
        process = _make_mock_process([SESSION_EVENT, AGENT_END_ERROR_EVENT],
                                     stderr=noise.encode("utf-8"), returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await shell.health_check(cwd="/tmp")

        # Assert
        assert result.healthy is False
        assert result.exception == "500 model name=qwen3.6-27b-8Q failed to load"

    async def test_health_check_reports_the_same_reason_for_the_same_stream(self):
        # Arrange — execute() and health_check() judge one normalized stream by one rule, so
        # the same failure has to read identically whichever surface a caller used.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        reason = "Cannot use this model: bogus."
        process = _make_mock_process([SYSTEM_EVENT, RESULT_EVENT_ERROR],
                                     stderr=reason.encode("utf-8"), returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await shell.health_check(cwd="/tmp")

        # Assert
        assert result.healthy is False
        assert result.exception == reason


class TestOkResultWithNonZeroExit:
    """A good answer plus a non-zero exit: DELIBERATELY a failure. Do not soften this.

    Node-based CLIs write warnings to stderr all the time, so it is tempting to let the "ok"
    result win. But the `error` event only exists when the child ALSO exited non-zero —
    stderr alone never produces one — and a non-zero exit is the CLI stating outright that
    the invocation failed. health.py has always called that unhealthy; letting execute()
    call the same stream a success would split the two surfaces apart, which is the one
    thing outcome.py exists to prevent. The answer is not thrown away: it rides on the
    exception, so a caller who disagrees can still read it.
    """

    async def test_an_ok_result_does_not_survive_a_non_zero_exit(self):
        # Arrange — a complete, successful pi turn whose process then exits 1 with a node
        # warning on stderr.
        shell = AgentShell(agent_type=AgentType.PI)
        warning = "(node:41273) ExperimentalWarning: WASI is an experimental feature"
        process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT],
                                     stderr=warning.encode("utf-8"), returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        # Assert — reported with the only detail available, and the answer is not destroyed.
        assert str(excinfo.value) == warning
        assert excinfo.value.response == "PONG"
        assert excinfo.value.output_tokens == 27

    async def test_health_check_calls_the_same_stream_unhealthy(self):
        # Arrange — the consistency argument, made executable.
        shell = AgentShell(agent_type=AgentType.PI)
        warning = "(node:41273) ExperimentalWarning: WASI is an experimental feature"
        process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT],
                                     stderr=warning.encode("utf-8"), returncode=1)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await shell.health_check(cwd="/tmp")

        # Assert
        assert result.healthy is False
        assert result.exception == warning

    async def test_stderr_on_a_clean_exit_is_not_a_failure_at_all(self):
        # Arrange — the boundary that keeps the rule above from being over-broad: the very
        # same node warning with returncode 0 emits no `error` event, so the run succeeds.
        shell = AgentShell(agent_type=AgentType.PI)
        warning = "(node:41273) ExperimentalWarning: WASI is an experimental feature"
        process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT],
                                     stderr=warning.encode("utf-8"), returncode=0)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            response = await shell.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.response == "PONG"


class TestMoreThanOneResultEvent:
    """Which `result` event decides, when one run emitted several.

    pi really does emit more than one agent_end per invocation: auto-retry is on by default
    (maxRetries 3) and auto-compaction takes the same `agent.continue()` path, and each
    continuation runs its own agent loop with its own agent_end. So "a failing result
    arrived" cannot mean "the run failed" — the LAST result is the verdict, the same way
    the collector already takes its metrics from the last one.
    """

    async def test_a_failure_the_agent_retried_past_is_not_a_failure(self):
        # Arrange — pi hits a transient 500, auto-retries and answers. Two agent_end events,
        # error then ok. Treating any failing result as fatal turns a run the agent itself
        # recovered from into a raise.
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_mock_process([SESSION_EVENT, AGENT_END_RETRY_PENDING_EVENT,
                                      TEXT_END_UPDATE, AGENT_END_RETRY_SUCCEEDED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            response = await shell.execute(cwd="/tmp", prompt="ping")

        # Assert — and the metrics are the surviving attempt's, not the abandoned one's.
        assert response.response == "PONG"
        assert response.output_tokens == 31
        assert response.cost == 0.005

    async def test_health_check_agrees_a_retried_run_is_healthy(self):
        # Arrange — same stream, other surface: one rule, one verdict.
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_mock_process([SESSION_EVENT, AGENT_END_RETRY_PENDING_EVENT,
                                      TEXT_END_UPDATE, AGENT_END_RETRY_SUCCEEDED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await shell.health_check(cwd="/tmp")

        # Assert
        assert result.healthy is True
        assert result.exception is None

    async def test_a_failure_after_a_good_result_still_fails_the_run(self):
        # Arrange — the mirror image, and why "any ok result means success" is equally wrong:
        # the first loop answered, the continuation (compaction/follow-up) then errored. The
        # run ended badly, so it must raise. Ordering is the whole rule.
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT,
                                      AGENT_END_ERROR_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        assert str(excinfo.value) == "500 model name=qwen3.6-27b-8Q failed to load"

    async def test_the_first_failing_results_reason_is_the_one_reported(self):
        # Arrange — retry budget exhausted: every attempt failed, each with its own reason.
        # The earliest is the root cause; a later one is what the run degenerated into.
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_mock_process([SESSION_EVENT, AGENT_END_RETRY_PENDING_EVENT,
                                      AGENT_END_ERROR_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        assert str(excinfo.value) == "500 Internal Server Error"


class TestErrorEventWithoutText:
    """An `error` event that carries no text must still read as a failure, not as silence."""

    async def test_empty_error_content_is_reported_as_unknown_error(self):
        # Arrange — opencode's error envelope with a blank `data.message`: the adapter's own
        # "Unknown error" default does not fire (the key exists), so the event arrives empty.
        # Passing that through would report the run as "no result event received", blaming
        # the wrong thing — the harness did say it failed, it just did not say why.
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        process = _make_mock_process([ERROR_EVENT_EMPTY_MESSAGE])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        assert str(excinfo.value) == "unknown error"


class TestMissingResultFailure:
    """Shape 3 — the stream ended with no terminal `result` at all."""

    async def test_stream_without_a_terminal_result_raises(self):
        # Arrange — a killed or truncated run: text and session arrived, the result never did,
        # and the process still exited 0 with a silent stderr.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        process = _make_mock_process([SYSTEM_EVENT, TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await shell.execute(cwd="/tmp", prompt="ping")

        # Assert — same wording as the health probe, and the partial output is preserved.
        error = excinfo.value
        assert str(error) == "no result event received"
        assert error.response == "Hey! Here's some text output."
        assert error.session_id == "test-session"
        assert error.cost == 0.0
        assert error.output_tokens == 0


class TestEveryAdapterObeysTheContract:
    """The rule is normalized, so it must hold identically for every adapter."""

    @pytest.mark.parametrize("adapter_cls", ADAPTERS)
    async def test_ok_result_returns_a_response(self, adapter_cls):
        # Arrange
        adapter = adapter_cls()
        process = _make_mock_process([OK_RESULT_EVENT[adapter_cls]])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert isinstance(response, AgentResponse)

    @pytest.mark.parametrize("adapter_cls", ADAPTERS)
    async def test_error_event_raises(self, adapter_cls):
        # Arrange — a non-zero exit with stderr is the one failure path every CLI shares.
        adapter = adapter_cls()
        process = _make_mock_process([OK_RESULT_EVENT[adapter_cls]], stderr=b"spawn failed",
                                     returncode=1)

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError, match="spawn failed"):
                await adapter.execute(cwd="/tmp", prompt="ping")

    @pytest.mark.parametrize("adapter_cls", ADAPTERS)
    async def test_missing_result_raises(self, adapter_cls):
        # Arrange — nothing at all on stdout and a clean exit: the only failure signal left.
        adapter = adapter_cls()
        process = _make_mock_process([])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError, match="no result event received"):
                await adapter.execute(cwd="/tmp", prompt="ping")

    @pytest.mark.parametrize("adapter_cls", ERROR_RESULT_ADAPTERS)
    async def test_error_result_raises(self, adapter_cls):
        # Arrange — only four CLIs can report failure on their terminal event.
        adapter = adapter_cls()
        process = _make_mock_process([ERROR_RESULT_EVENT[adapter_cls]])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(AgentExecutionError):
                await adapter.execute(cwd="/tmp", prompt="ping")
