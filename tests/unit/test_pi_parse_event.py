from agent_shell.adapters.pi_adapter import PiAdapter

from tests.unit.pi_fixtures import (
    SESSION_EVENT,
    AGENT_START_EVENT,
    TURN_START_EVENT,
    TURN_END_EVENT,
    MESSAGE_START_USER_EVENT,
    MESSAGE_END_USER_EVENT,
    MESSAGE_END_ASSISTANT_EVENT,
    THINKING_START_UPDATE,
    THINKING_DELTA_UPDATE,
    THINKING_END_UPDATE,
    THINKING_END_EMPTY_UPDATE,
    TEXT_START_UPDATE,
    TEXT_DELTA_UPDATE,
    TEXT_END_UPDATE,
    TEXT_END_EMPTY_UPDATE,
    TOOL_EXECUTION_START_EVENT,
    TOOL_EXECUTION_UPDATE_EVENT,
    TOOL_EXECUTION_END_EVENT,
    AGENT_END_TEXT_EVENT,
    AGENT_END_TOOLUSE_EVENT,
    AGENT_END_ERROR_EVENT,
    AGENT_END_ERROR_NO_MESSAGE_EVENT,
    AGENT_END_ABORTED_EVENT,
    UNKNOWN_EVENT,
)


class TestParseEventSession:
    def test_emits_system_event_with_session_id(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(SESSION_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "system"
        assert events[0].session_id == "019f0ae6-995e-780b-b2e7-f00d2d72873f"

    def test_skips_session_without_id(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event({"type": "session"}, include_thinking=False)

        # Assert
        assert events == []


class TestParseEventText:
    def test_emits_text_on_text_end(self):
        # Arrange — text is surfaced on text_end (full block), not per-delta.
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(TEXT_END_UPDATE, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "PONG"

    def test_skips_empty_text_end(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(TEXT_END_EMPTY_UPDATE, include_thinking=False)

        # Assert
        assert events == []

    def test_skips_text_start_and_text_delta(self):
        # Arrange — deltas would corrupt execute()'s newline-join; only _end is surfaced.
        adapter = PiAdapter()

        # Act / Assert
        assert adapter._parse_event(TEXT_START_UPDATE, include_thinking=False) == []
        assert adapter._parse_event(TEXT_DELTA_UPDATE, include_thinking=False) == []


class TestParseEventThinking:
    def test_emits_thinking_on_thinking_end_when_included(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(THINKING_END_UPDATE, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert "PONG" in events[0].content

    def test_skips_thinking_end_when_not_included(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(THINKING_END_UPDATE, include_thinking=False)

        # Assert
        assert events == []

    def test_skips_thinking_start_and_delta_even_when_included(self):
        # Arrange
        adapter = PiAdapter()

        # Act / Assert
        assert adapter._parse_event(THINKING_START_UPDATE, include_thinking=True) == []
        assert adapter._parse_event(THINKING_DELTA_UPDATE, include_thinking=True) == []

    def test_skips_empty_thinking_end(self):
        # Arrange — symmetric with the empty text_end guard.
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(THINKING_END_EMPTY_UPDATE, include_thinking=True)

        # Assert
        assert events == []


class TestParseEventToolUse:
    def test_emits_tool_use_on_tool_execution_start(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(TOOL_EXECUTION_START_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "tool_use"
        assert events[0].content == "bash"

    def test_skips_tool_execution_update_and_end(self):
        # Arrange — one tool_use per call, on start only.
        adapter = PiAdapter()

        # Act / Assert
        assert adapter._parse_event(TOOL_EXECUTION_UPDATE_EVENT, include_thinking=False) == []
        assert adapter._parse_event(TOOL_EXECUTION_END_EVENT, include_thinking=False) == []


class TestParseEventAgentEnd:
    def test_emits_ok_result_with_summed_output_tokens(self):
        # Arrange
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(AGENT_END_TEXT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].cost == 0.0
        assert events[0].output_tokens == 27

    def test_sums_output_tokens_across_assistant_turns(self):
        # Arrange — tool-use run has two assistant turns (47 + 8).
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(AGENT_END_TOOLUSE_EVENT, include_thinking=False)

        # Assert
        assert events[0].output_tokens == 55

    def test_sums_cost_across_assistant_turns(self):
        # Arrange — local model reports cost 0; a paid provider would not. Inline synthetic
        # event guards the cost-summation path (real values come from paid providers).
        adapter = PiAdapter()
        event = {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "a"}],
                 "usage": {"output": 10, "cost": {"total": 0.012}}, "stopReason": "stop"},
                {"role": "assistant", "content": [{"type": "text", "text": "b"}],
                 "usage": {"output": 5, "cost": {"total": 0.003}}, "stopReason": "stop"},
            ],
            "willRetry": False,
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert events[0].output_tokens == 15
        assert events[0].cost == 0.015

    def test_emits_error_result_when_assistant_stop_reason_error(self):
        # Arrange — pi exits 0 on a model error; failure shows as stopReason="error".
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(AGENT_END_ERROR_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "error"
        assert events[0].output_tokens == 0

    def test_error_result_carries_the_failing_messages_error_message(self):
        # Arrange — the real reason lives in the failing assistant message's
        # `errorMessage`; without it a consumer only ever learns "it failed" (issue #10).
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(AGENT_END_ERROR_EVENT, include_thinking=False)

        # Assert
        assert events[0].error == "500 model name=qwen3.6-27b-8Q failed to load"

    def test_error_result_falls_back_to_stop_reason_and_model_identity(self):
        # Arrange — `errorMessage` is optional on pi's type. When absent, the stopReason
        # plus provider/model is still far more actionable than a bare "error". Asserted as
        # the whole string, not by substring: a reason a human reads has to name the
        # provider before the model it served, and substring checks pass either way round.
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(
            AGENT_END_ERROR_NO_MESSAGE_EVENT, include_thinking=False
        )

        # Assert
        assert events[0].content == "error"
        assert events[0].error == "error (provider=llama-ai-server, model=qwen3.6-27b-8Q)"

    def test_ok_result_carries_no_error(self):
        # Arrange — a clean run must leave `error` unset, so its presence is a
        # reliable failure signal.
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(AGENT_END_TEXT_EVENT, include_thinking=False)

        # Assert
        assert events[0].content == "ok"
        assert events[0].error is None

    def test_aborted_stop_reason_is_a_failure(self):
        # Arrange — pi's StopReason union is stop|length|toolUse|error|aborted. An
        # aborted turn never completed, so reporting "ok" for it is wrong.
        adapter = PiAdapter()

        # Act
        events = adapter._parse_event(AGENT_END_ABORTED_EVENT, include_thinking=False)

        # Assert
        assert events[0].content == "error"
        assert "aborted" in events[0].error
        assert events[0].output_tokens == 4

    def test_only_the_last_assistant_turn_decides_the_status(self):
        # Arrange — this used to be an OR-fold: ANY errored turn made the whole agent_end an
        # error. That was harmless while the status was a field nobody read, but under issue
        # #11 it raises, so a stale turn could condemn a run that finished fine. pi itself
        # judges an agent_end by its last assistant message (agent-session.js
        # `_willRetryAfterAgentEnd`, and print-mode's text output), so we do too. Tokens and
        # cost still sum over every turn — agent_end carries only THIS run's messages, so
        # they all belong to the caller's bill.
        adapter = PiAdapter()
        event = {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [],
                 "usage": {"output": 12, "cost": {"total": 0.001}}, "stopReason": "error"},
                {"role": "toolResult", "content": [{"type": "text", "text": "x"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                 "usage": {"output": 8, "cost": {"total": 0.002}}, "stopReason": "stop"},
            ],
            "willRetry": False,
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert events[0].content == "ok"
        assert events[0].error is None
        assert events[0].output_tokens == 20
        assert events[0].cost == 0.003

    def test_the_last_assistant_turn_is_found_behind_a_trailing_tool_result(self):
        # Arrange — a run that stopped straight after tool execution ends on a toolResult,
        # which has no stopReason at all. Reading messages[-1] blindly would see no failure
        # signal; the current turn is the last ASSISTANT message.
        adapter = PiAdapter()
        event = {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [], "model": "m", "provider": "p",
                 "usage": {"output": 5, "cost": {"total": 0.0}},
                 "stopReason": "aborted"},
                {"role": "toolResult", "content": [{"type": "text", "text": "x"}]},
            ],
            "willRetry": False,
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert events[0].content == "error"
        assert "aborted" in events[0].error

    def test_the_current_turns_reason_is_the_one_reported(self):
        # Arrange — several failing turns in one agent_end. The reason a caller is given
        # describes the turn that ended the run, not one the agent already moved past.
        adapter = PiAdapter()
        event = {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [], "model": "m", "provider": "p",
                 "usage": {"output": 1, "cost": {"total": 0.0}},
                 "stopReason": "error", "errorMessage": "Connection error."},
                {"role": "assistant", "content": [], "model": "m", "provider": "p",
                 "usage": {"output": 2, "cost": {"total": 0.0}},
                 "stopReason": "error", "errorMessage": "Request timed out."},
            ],
            "willRetry": False,
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert events[0].error == "Request timed out."
        assert events[0].output_tokens == 3

    def test_agent_end_carrying_no_assistant_turn_is_not_an_error(self):
        # Arrange — defensive: an agent_end with nothing but user/tool messages has no turn
        # to judge, so it must not be invented into a failure.
        adapter = PiAdapter()
        event = {
            "type": "agent_end",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "willRetry": False,
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert events[0].content == "ok"
        assert events[0].error is None

    def test_agent_end_without_usage_defaults_to_zero(self):
        # Arrange — be defensive against a missing/null usage object.
        adapter = PiAdapter()
        event = {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [], "stopReason": "stop"},
                {"role": "assistant", "usage": None, "content": [], "stopReason": "stop"},
            ],
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].output_tokens == 0
        assert events[0].cost == 0.0


class TestParseEventIgnored:
    def test_ignores_lifecycle_and_unknown_events(self):
        # Arrange
        adapter = PiAdapter()
        skipped = [
            AGENT_START_EVENT,
            TURN_START_EVENT,
            TURN_END_EVENT,
            MESSAGE_START_USER_EVENT,
            MESSAGE_END_USER_EVENT,
            MESSAGE_END_ASSISTANT_EVENT,
            UNKNOWN_EVENT,
            {"foo": "bar"},
        ]

        # Act / Assert
        for event in skipped:
            assert adapter._parse_event(event, include_thinking=False) == [], event
