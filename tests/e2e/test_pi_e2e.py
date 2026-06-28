import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


# Pi E2E uses the locally-configured default provider/model (no cloud key needed).
# These are slow against a local model; they are a local smoke test, not CI.


class TestStreamE2E:
    async def test_stream_yields_text_result_and_session_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

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
        shell = AgentShell(agent_type=AgentType.PI)

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
        # Canary: a real run must report generated tokens. Fails the moment Pi renames or
        # drops the assistant message's usage.output field — the silent-degrade-to-0 bug.
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Write a short paragraph about the sea.",
        )

        # Assert
        assert response.output_tokens > 0, (
            "No output tokens from a real run — Pi's usage.output field may have been "
            "renamed/dropped; re-verify agent_end message usage in the adapter"
        )


class TestDisallowedToolsE2E:
    async def test_exclude_tools_is_accepted_and_bash_unavailable(self):
        # Regression guard for the deny mechanism: `--exclude-tools bash`. If a future pi
        # renames/removes the flag, the run errors and the deny silently no-ops. Unit tests
        # only assert agent_shell emits the flag; only this real run proves pi accepts it AND
        # that the excluded tool is genuinely unavailable to the model.
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Use your bash tool to run `echo HELLO`, then reply DONE.",
            disallowed_tools=["bash"],
        ):
            events.append(event)

        # Assert — pi accepted the flag (no error, completed) and never ran the bash tool.
        error_events = [e for e in events if e.type == "error"]
        result_events = [e for e in events if e.type == "result"]
        bash_calls = [e for e in events if e.type == "tool_use" and e.content == "bash"]
        assert not error_events, (
            f"pi rejected --exclude-tools (possible flag rename): "
            f"{[e.content for e in error_events]}"
        )
        assert len(result_events) == 1
        assert not bash_calls, "bash was excluded but the model still invoked it"


class TestSessionResumeE2E:
    async def test_resume_session_with_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)

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
