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


class TestDisallowedToolsE2E:
    async def test_bash_deny_blocks_shell_under_skip_permissions(self, tmp_path):
        # Regression guard for the load-bearing OpenCode deny path. The adapter passes
        # --dangerously-skip-permissions (auto_approve defaults True), and the object-form
        # OPENCODE_PERMISSION `deny` must STILL block the tool. This holds only because a deny
        # rule raises DeniedError before the `permission.asked` event the flag auto-approves
        # (opencode 1.14.41, permission/index.ts). An upgrade that reworks the permission engine
        # could silently turn the deny into a no-op (the dev branch already ships a v2 engine),
        # and the bare-string-vs-object merge quirk is fragile. Unit tests only prove agent_shell
        # EMITS the env var; only this real run proves opencode ENFORCES it. Re-run on upgrade.
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        marker = tmp_path / "should_not_exist.txt"

        # Act — ask the agent to actually run a shell command that writes the marker, bash denied.
        async for _ in shell.stream(
            cwd=str(tmp_path),
            prompt=(
                f"Use the bash/shell tool to run exactly this command: echo DENIED > {marker.name}. "
                "Actually execute it as a tool call; do not just describe it."
            ),
            disallowed_tools=["bash"],
        ):
            pass

        # Assert — the shell tool was denied, so the command never executed and the file is absent.
        assert not marker.exists(), (
            "opencode executed a denied bash tool under --dangerously-skip-permissions; "
            "deny enforcement regressed — check the opencode permission engine/version"
        )
