import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


class TestStreamE2E:
    async def test_stream_yields_text_and_result_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

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

    async def test_stream_with_thinking_yields_thinking_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            effort="high",
            include_thinking=True,
        ):
            events.append(event)

        # Assert
        thinking_events = [e for e in events if e.type == "thinking"]
        assert len(thinking_events) >= 1, "Expected at least one thinking event"

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="List the files in the current directory using the Bash tool",
            allowed_tools=["Bash"],
        ):
            events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected at least one tool_use event"


class TestAutoApproveE2E:
    async def test_stream_uses_tools_with_default_auto_approve(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="Use the Bash tool to echo 'auto approved'",
            allowed_tools=["Bash"],
        ):
            events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) >= 1, "Expected tool use with default auto_approve=True"

    async def test_execute_completes_with_auto_approve_disabled(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: no tools needed",
            allowed_tools=[],
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response"


class TestExecuteE2E:
    async def test_execute_returns_response_with_text(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost == 0.0, "Expected cost to be 0.0 (Copilot has no pricing)"

    async def test_execute_with_effort(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            effort="high",
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"


class TestSessionE2E:
    async def test_stream_returns_session_id(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

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
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

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
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act - first call to get a session_id
        first_response = await shell.execute(
            cwd="/tmp",
            prompt="Remember the word 'banana'",
            allowed_tools=[],
        )

        # Act - resume with session_id
        second_response = await shell.execute(
            cwd="/tmp",
            prompt="What word did I ask you to remember?",
            allowed_tools=[],
            session_id=first_response.session_id,
        )

        # Assert
        assert isinstance(second_response, AgentResponse)
        assert len(second_response.response) > 0


class TestOutputTokensE2E:
    async def test_execute_reports_output_tokens(self):
        # Canary: a real run must report generated tokens. Fails the moment Copilot renames or
        # drops assistant.message.data.outputTokens — the silent-degrade-to-0 bug to catch.
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Write a short paragraph about the sea.",
            allowed_tools=[],
        )

        # Assert
        assert response.output_tokens > 0, (
            "No output tokens from a real run — the CLI's usage field may have been "
            "renamed/dropped; re-verify assistant.message.data.outputTokens in the adapter"
        )

    async def test_multistep_accumulates_output_tokens(self, tmp_path):
        # The live counterpart to the unit accumulation guard: a real tool-using run must sum
        # output across every assistant.message, not just the final one. Only this proves
        # accumulation works against Copilot's actual multi-message event stream.
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        response = await shell.execute(
            cwd=str(tmp_path),
            prompt=(
                "Create one.txt containing 'alpha', create two.txt containing 'beta', "
                "read both back, then tell me the two words."
            ),
        )

        # Assert — loose plausibility floor: a take-last regression would cap this at the final
        # message's output (tens of tokens), well under 100.
        assert response.output_tokens > 100, (
            "Multi-step output tokens implausibly low — accumulation across assistant.message "
            "events likely regressed to take-last"
        )
