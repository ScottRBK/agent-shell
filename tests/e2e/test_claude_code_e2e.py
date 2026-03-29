import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


class TestStreamE2E:
    async def test_stream_yields_text_and_result_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            model="haiku",
        ):
            events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"
        assert result_events[0].cost > 0, "Expected cost to be greater than 0"
        assert result_events[0].duration > 0, "Expected duration to be greater than 0"

    async def test_stream_with_thinking_yields_thinking_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            model="haiku",
            effort="high",
            include_thinking=True,
        ):
            events.append(event)

        # Assert
        thinking_events = [e for e in events if e.type == "thinking"]
        assert len(thinking_events) >= 1, "Expected at least one thinking event"

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="List the files in the current directory using the Bash tool",
            allowed_tools=["Bash"],
            model="haiku",
        ):
            events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected at least one tool_use event"


class TestAutoApproveE2E:
    async def test_stream_uses_tools_with_default_auto_approve(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Use the Bash tool to echo 'auto approved'",
            allowed_tools=["Bash"],
            model="haiku",
        ):
            events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected tool use with default auto_approve=True"

    async def test_execute_completes_with_auto_approve_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: no tools needed",
            allowed_tools=[],
            model="haiku",
            auto_approve=False,
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response"


class TestExecuteE2E:
    async def test_execute_returns_response_with_text_and_cost(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            model="haiku",
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost > 0, "Expected cost to be greater than 0"

    async def test_execute_with_effort(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            model="haiku",
            effort="high",
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost > 0, "Expected cost to be greater than 0"


class TestSessionE2E:
    async def test_stream_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Respond with exactly: hello",
            allowed_tools=[],
            model="haiku",
        ):
            events.append(event)

        # Assert
        session_events = [e for e in events if e.session_id]
        assert len(session_events) >= 1, "Expected at least one event with session_id"
        assert isinstance(session_events[0].session_id, str)
        assert len(session_events[0].session_id) > 0

    async def test_execute_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello",
            allowed_tools=[],
            model="haiku",
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.session_id is not None
        assert len(response.session_id) > 0

    async def test_resume_session_with_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act - first call to get a session_id
        first_response = await shell.execute(
            cwd="/tmp",
            prompt="Remember the word 'banana'",
            allowed_tools=[],
            model="haiku",
        )

        # Act - resume with session_id
        second_response = await shell.execute(
            cwd="/tmp",
            prompt="What word did I ask you to remember?",
            allowed_tools=[],
            model="haiku",
            session_id=first_response.session_id,
        )

        # Assert
        assert isinstance(second_response, AgentResponse)
        assert len(second_response.response) > 0
