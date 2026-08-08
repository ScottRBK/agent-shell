import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


# Real `grok` binary + API. Local smoke only (excluded from CI via the e2e marker).


class TestStreamE2E:
    async def test_stream_yields_text_result_and_session_events(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)

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
        shell = AgentShell(agent_type=AgentType.GROK)

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


class TestCostAndTokensE2E:
    async def test_execute_reports_output_tokens(self):
        # Canary: a real run must report billed output tokens. Cost may legitimately be
        # 0.0 on some OAuth/pool paths (Grok docs), so cost is not asserted > 0.
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Write a short paragraph about the sea.",
        )

        # Assert
        assert response.output_tokens > 0, (
            "No output tokens from a real run — check result.usage.output_tokens "
            "mapping in the adapter (do not add reasoning_tokens; it is a subset)"
        )
        assert response.cost >= 0.0


class TestDisallowedToolsE2E:
    async def test_canonical_bash_deny_removes_shell_tool_from_init(self):
        # Safety canary: the adapter's bash mapping must make the shell tool disappear
        # from a real system/init.tools list. Live check on grok 1.0.0 showed
        # --disallowed-tools run_terminal_command is a no-op while run_terminal_cmd works.
        import json
        import asyncio

        from agent_shell.adapters.grok_adapter import _DISALLOWED_TOOL_MAP

        # Arrange
        native = ",".join(_DISALLOWED_TOOL_MAP["bash"])
        cmd = [
            "grok", "-p", "Reply with exactly: ok",
            "--output-format", "streaming-messages-json",
            "--always-approve",
            "--disallowed-tools", native,
        ]

        # Act
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd="/tmp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        lines = [ln for ln in stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        assert lines, "grok produced no stdout"
        init = json.loads(lines[0])

        # Assert
        assert init.get("type") == "system" and init.get("subtype") == "init"
        tools = init.get("tools") or []
        assert "run_terminal_command" not in tools
        assert "run_terminal_cmd" not in tools


class TestSessionResumeE2E:
    async def test_resume_returns_the_same_session_id(self):
        # Session-id identity from the CLI's own end.sessionId field.
        # Arrange
        shell = AgentShell(agent_type=AgentType.GROK)

        # Act
        first = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
        )
        resumed = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            session_id=first.session_id,
        )
        fresh = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
        )

        # Assert
        assert isinstance(resumed, AgentResponse)
        assert resumed.session_id == first.session_id, (
            "--resume did not continue the session"
        )
        assert fresh.session_id != first.session_id, (
            "a session-less run reused the id"
        )
