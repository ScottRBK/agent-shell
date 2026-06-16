import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


# Codex E2E uses gpt-5.4-mini explicitly to keep token costs low.
MODEL = "gpt-5.4-mini"


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
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert "PONG" in response.response
        assert response.cost == 0.0
        assert response.session_id is not None
        assert len(response.session_id) > 0


class TestSessionResumeE2E:
    async def test_resume_session_with_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)

        # Act — first turn establishes a session
        first = await shell.execute(
            cwd="/tmp",
            prompt="Remember the word 'banana'. Reply with just 'OK'.",
            model=MODEL,
        )

        # Act — resume on the captured session_id
        second = await shell.execute(
            cwd="/tmp",
            prompt="What word did I just ask you to remember? Reply with just that word.",
            model=MODEL,
            session_id=first.session_id,
        )

        # Assert
        assert isinstance(second, AgentResponse)
        assert "banana" in second.response.lower()


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
