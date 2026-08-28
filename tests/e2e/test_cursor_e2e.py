import pytest

from agent_shell.models.agent import AgentResponse, AgentType, StreamEvent
from agent_shell.shell import AgentShell

pytestmark = pytest.mark.e2e


# Cursor advertises named models that a Free plan cannot execute, and an omitted model can
# inherit a previously selected named model. Pin Auto so the smoke test exercises an executable
# model instead of depending on mutable account state.
MODEL = "auto"


# These call the real cursor-agent binary and API; they are a local smoke test, not CI.
# Running in /tmp (an untrusted dir) also proves the mandatory --trust flag is accepted: without
# it cursor-agent would exit 1 before emitting any events.


class TestStreamE2E:
    async def test_stream_yields_text_result_and_session_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Reply with exactly the word PONG and nothing else.",
            model=MODEL,
        ):
            events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]
        session_events = [e for e in events if e.session_id]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"
        assert result_events[0].content == "ok"
        assert len(session_events) >= 1, "Expected at least one event with session_id"


class TestExecuteE2E:
    async def test_execute_returns_response_with_text_and_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Reply with exactly the word PONG and nothing else.",
            model=MODEL,
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert "PONG" in response.response.upper()
        assert response.session_id is not None
        assert len(response.session_id) > 0


class TestOutputTokensE2E:
    async def test_execute_reports_output_tokens(self):
        # Canary: a real run must report generated tokens. usage.outputTokens is UNDOCUMENTED,
        # so this fails the moment Cursor renames or drops it — the silent-degrade-to-0 bug.
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Write a short paragraph about the sea.",
            model=MODEL,
        )

        # Assert
        assert response.output_tokens > 0, (
            "No output tokens from a real run — Cursor's usage.outputTokens field may have "
            "been renamed/dropped; re-verify the result event usage in the adapter"
        )


class TestSessionResumeE2E:
    async def test_resume_returns_the_same_session_id(self):
        # Regression guard for the `--resume=<id>` form. Checked on session-id identity rather
        # than by asking the model to recall a planted word: the id is parsed out of the CLI's
        # OWN json stream (event session_id), so it is cursor-agent reporting which session it
        # ran, not an assertion about model cooperation.
        #
        # CAVEAT, measured: unlike claude-code/codex/copilot/opencode, cursor-agent ACCEPTS an
        # unknown id — `--resume=<never-seen-uuid>` starts a session under that id and echoes
        # it back. So identity here proves the adapter passed the flag through and cursor
        # honoured it; it is not by itself proof a prior transcript was replayed. It still
        # catches the regressions that matter to us: drop or rename the flag and cursor mints
        # its own id instead, failing the first assert.
        #
        # The `fresh` leg is load-bearing — without it an adapter that returned a constant id
        # would pass.
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act
        first = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            model=MODEL,
        )
        resumed = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            model=MODEL,
            session_id=first.session_id,
        )
        fresh = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            model=MODEL,
        )

        # Assert
        assert isinstance(resumed, AgentResponse)
        assert resumed.session_id == first.session_id, "--resume= did not continue the session"
        assert fresh.session_id != first.session_id, "a session-less run reused the id"
