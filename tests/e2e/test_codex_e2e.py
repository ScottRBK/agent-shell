import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


# Codex E2E uses gpt-5.6-luna explicitly to keep token costs low.
MODEL = "gpt-5.6-luna"


class TestStreamE2E:
    async def test_stream_yields_text_and_result_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Reply with exactly the word PONG and nothing else.",
            model=MODEL,
            effort="low",
        ):
            events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]
        session_events = [e for e in events if e.session_id]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"
        assert len(session_events) >= 1, "Expected at least one event with session_id"


class TestExecuteE2E:
    async def test_execute_returns_response_with_text_and_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Reply with exactly the word PONG and nothing else.",
            model=MODEL,
            effort="low",
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert "PONG" in response.response
        assert response.cost == 0.0
        assert response.session_id is not None
        assert len(response.session_id) > 0


class TestSessionResumeE2E:
    async def test_resume_returns_the_same_session_id(self):
        # Resume is checked on session-id identity, not on asking the model to recall a
        # planted word: the id is parsed out of the CLI's OWN json stream
        # (thread.started.thread_id), so an id that survives `codex exec resume <id>` is codex
        # itself confirming it continued that thread. Verified against codex: an unknown id is
        # rejected ("no rollout found for thread id"), so the match cannot be an echo of what
        # we passed in. The `fresh` leg is load-bearing — without it an adapter that returned a
        # constant id would pass. Note codex takes the id as a subcommand argument, not a flag.
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)

        # Act
        first = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            model=MODEL,
            effort="low",
        )
        resumed = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            model=MODEL,
            effort="low",
            session_id=first.session_id,
        )
        fresh = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            model=MODEL,
            effort="low",
        )

        # Assert
        assert isinstance(resumed, AgentResponse)
        assert resumed.session_id == first.session_id, "resume did not continue the thread"
        assert fresh.session_id != first.session_id, "a session-less run reused the id"


class TestDisallowedToolsE2E:
    async def test_web_search_deny_config_is_accepted_by_codex(self):
        # Regression guard for the one Codex deny mechanism: `-c web_search="disabled"`.
        # If a future Codex renames/removes this top-level config key, the run errors with
        # "unknown configuration field" and the deny silently becomes a no-op. Unit tests
        # only assert agent_shell emits the string; only this real run proves Codex accepts
        # it. Arrange / Act
        shell = AgentShell(agent_type=AgentType.CODEX)
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Reply with exactly the word PONG and nothing else.",
            model=MODEL,
            effort="low",
            disallowed_tools=["web_search"],
        ):
            events.append(event)

        # Assert — codex accepted the config (no error) and completed the turn.
        error_events = [e for e in events if e.type == "error"]
        result_events = [e for e in events if e.type == "result"]
        assert not error_events, (
            "codex rejected the web_search deny config "
            f"(possible upstream key rename): {[e.content for e in error_events]}"
        )
        assert len(result_events) == 1


class TestOutputTokensE2E:
    async def test_execute_reports_output_tokens(self):
        # Canary: a real run must report generated tokens. Fails the moment Codex renames or
        # drops turn.completed.usage.output_tokens — the silent-degrade-to-0 bug to catch.
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Write a short paragraph about the sea.",
            model=MODEL,
            effort="low",
        )

        # Assert
        assert response.output_tokens > 0, (
            "No output tokens from a real run — the CLI's usage field may have been "
            "renamed/dropped; re-verify turn.completed.usage.output_tokens in the adapter"
        )
