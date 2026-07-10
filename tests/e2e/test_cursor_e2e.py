import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


# Cursor E2E uses the account's default model (on a Free plan that is Auto), so no model is
# passed. These call the real cursor-agent binary and API; they are a local smoke test, not CI.
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
        )

        # Assert
        assert response.output_tokens > 0, (
            "No output tokens from a real run — Cursor's usage.outputTokens field may have "
            "been renamed/dropped; re-verify the result event usage in the adapter"
        )


class TestSessionResumeE2E:
    async def test_resume_session_with_session_id(self):
        # Regression guard for the `--resume=<id>` form and session continuity.
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Act — first turn establishes a session
        first = await shell.execute(
            cwd="/tmp",
            prompt="Remember the word 'banana'. Reply with just 'OK'.",
        )

        # Act — resume on the captured session_id
        second = await shell.execute(
            cwd="/tmp",
            prompt="What word did I just ask you to remember? Reply with just that word.",
            session_id=first.session_id,
        )

        # Assert
        assert isinstance(second, AgentResponse)
        assert "banana" in second.response.lower()
