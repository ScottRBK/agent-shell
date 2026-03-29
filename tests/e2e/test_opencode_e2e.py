import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


class TestStreamE2E:
    async def test_stream_yields_text_and_result_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
        ):
            events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Use the bash tool to run: echo 'tool test'",
        ):
            events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected at least one tool_use event"


class TestExecuteE2E:
    async def test_execute_returns_response_with_text(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"


class TestSessionE2E:
    async def test_stream_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Respond with exactly: hello",
            allowed_tools=[],
        ):
            events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert len(session_events) >= 1, "Expected at least one event with session_id"
        assert isinstance(session_events[0].session_id, str)
        assert len(session_events[0].session_id) > 0

    async def test_execute_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello",
            allowed_tools=[],
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.session_id is not None
        assert len(response.session_id) > 0

    async def test_resume_session_with_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        first_response = await shell.execute(
            cwd="/tmp",
            prompt="Remember the word 'banana'",
            allowed_tools=[],
        )

        # Act
        second_response = await shell.execute(
            cwd="/tmp",
            prompt="What word did I ask you to remember?",
            allowed_tools=[],
            session_id=first_response.session_id,
        )

        # Assert
        assert isinstance(second_response, AgentResponse)
        assert len(second_response.response) > 0
