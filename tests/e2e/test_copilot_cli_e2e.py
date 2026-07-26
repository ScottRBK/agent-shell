"""Copilot CLI E2E tests — real CLI calls.

Every call pins its model explicitly. Without it the CLI falls back to whatever
`~/.copilot/settings.json` happens to hold, so a change to the developer's personal config
silently changes what these tests exercise.
"""

import os

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse, StreamEvent


pytestmark = pytest.mark.e2e


# Free-tier model, used wherever the test does not care which model answers.
# Lowercase only: the CLI rejects "Auto".
MODEL = "auto"

# 'auto' rejects --effort outright ("does not support reasoning effort configuration"),
# so the effort/thinking tests need a reasoning-capable model instead.
REASONING_MODEL = "gpt-5.3-codex"

# On the free tier `auto` is the ONLY selectable model — every named model, this one included,
# answers "not available" — and `auto` refuses --effort. So reasoning effort is untestable
# end-to-end without a paid subscription. Gated rather than deleted: the coverage is real, it
# just needs an account that can reach a named model. Set the var to run them.
requires_paid_tier = pytest.mark.skipif(
    not os.getenv("COPILOT_PAID_TIER"),
    reason=(
        "needs a paid Copilot tier: the free tier exposes only 'auto', which rejects "
        "--effort. Set COPILOT_PAID_TIER=1 to run."
    ),
)


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
            model=MODEL,
        ):
            events.append(event)

        # Assert
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"

    @requires_paid_tier
    async def test_stream_with_thinking_enabled_completes(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt=(
                "A box contains 3 red balls and 2 blue balls. Two balls are drawn without "
                "replacement. Determine the probability that both are red, then respond with "
                "only the reduced fraction."
            ),
            allowed_tools=[],
            model=REASONING_MODEL,
            effort="high",
            include_thinking=True,
        ):
            events.append(event)

        # Assert
        # Copilot can return opaque reasoning with an empty textual summary even when
        # reasoning summaries are requested. Integration tests cover the mapping from
        # non-empty reasoning events; this real-CLI test verifies the supported
        # model/effort/summary combination completes successfully.
        text_events = [e for e in events if e.type == "text"]
        result_events = [e for e in events if e.type == "result"]
        error_events = [e for e in events if e.type == "error"]

        assert len(text_events) >= 1, "Expected at least one text event"
        assert len(result_events) == 1, "Expected exactly one result event"
        assert error_events == []

    async def test_stream_with_tool_use(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        events: list[StreamEvent] = []
        async for event in shell.stream(
            cwd="/tmp",
            prompt="List the files in the current directory using the Bash tool",
            allowed_tools=["Bash"],
            model=MODEL,
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
            model=MODEL,
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
            model=MODEL,
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
            model=MODEL,
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert len(response.response) > 0, "Expected non-empty response text"
        assert response.cost == 0.0, "Expected cost to be 0.0 (Copilot has no pricing)"

    @requires_paid_tier
    async def test_execute_with_effort(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        response = await shell.execute(
            cwd="/tmp",
            prompt="Respond with exactly: hello world",
            allowed_tools=[],
            model=REASONING_MODEL,
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
            model=MODEL,
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
            model=MODEL,
        )

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.session_id is not None
        assert len(response.session_id) > 0

    async def test_resume_returns_the_same_session_id(self):
        # Resume is checked on session-id identity, not on asking the model to recall a
        # planted word: the id is parsed out of the CLI's OWN json stream (event sessionId),
        # so an id that survives `--resume` is copilot itself confirming it continued that
        # session. Verified against copilot: an unknown id is rejected ("No session, task, or
        # name matched"), so the match cannot be an echo of what we passed in. The `fresh` leg
        # is load-bearing — without it an adapter that returned a constant id would pass.
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Act
        first = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            allowed_tools=[],
            model=MODEL,
        )
        resumed = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            allowed_tools=[],
            model=MODEL,
            session_id=first.session_id,
        )
        fresh = await shell.execute(
            cwd="/tmp",
            prompt="Reply with just 'OK'.",
            allowed_tools=[],
            model=MODEL,
        )

        # Assert
        assert isinstance(resumed, AgentResponse)
        assert resumed.session_id == first.session_id, "--resume did not continue the session"
        assert fresh.session_id != first.session_id, "a session-less run reused the id"


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
            model=MODEL,
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
            model=MODEL,
        )

        # Assert — loose plausibility floor: a take-last regression would cap this at the final
        # message's output (tens of tokens), well under 100.
        assert response.output_tokens > 100, (
            "Multi-step output tokens implausibly low — accumulation across assistant.message "
            "events likely regressed to take-last"
        )
