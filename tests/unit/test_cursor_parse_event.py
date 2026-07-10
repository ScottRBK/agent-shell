from agent_shell.adapters.cursor_adapter import CursorAdapter

from tests.unit.cursor_fixtures import (
    SESSION_ID,
    SYSTEM_INIT_EVENT,
    SYSTEM_INIT_NO_SESSION_EVENT,
    USER_EVENT,
    THINKING_DELTA_EVENT,
    THINKING_DELTA_EMPTY_EVENT,
    THINKING_COMPLETED_EVENT,
    ASSISTANT_TEXT_EVENT,
    ASSISTANT_TEXT_EMPTY_EVENT,
    TOOL_CALL_SHELL_STARTED_EVENT,
    TOOL_CALL_SHELL_COMPLETED_EVENT,
    TOOL_CALL_MCP_STARTED_EVENT,
    TOOL_CALL_MCP_REJECTED_EVENT,
    RESULT_SUCCESS_EVENT,
    RESULT_ERROR_EVENT,
    RESULT_NO_USAGE_EVENT,
    UNKNOWN_EVENT,
)


class TestParseEventSession:
    def test_emits_system_event_with_session_id(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(SYSTEM_INIT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "system"
        assert events[0].session_id == SESSION_ID

    def test_skips_init_without_session_id(self):
        # Arrange — a system event with no session_id has nothing to carry.
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(SYSTEM_INIT_NO_SESSION_EVENT, include_thinking=False)

        # Assert
        assert events == []


class TestParseEventText:
    def test_emits_text_on_assistant_block(self):
        # Arrange — assistant events carry FULL text blocks (not per-token deltas).
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(ASSISTANT_TEXT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "PONG"

    def test_skips_empty_assistant_text(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(ASSISTANT_TEXT_EMPTY_EVENT, include_thinking=False)

        # Assert
        assert events == []

    def test_emits_text_per_block_for_multi_block_message(self):
        # Arrange — a single assistant message can carry several content blocks.
        adapter = CursorAdapter()
        event = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "A"},
                {"type": "text", "text": "B"},
            ]},
            "session_id": SESSION_ID,
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert [e.content for e in events] == ["A", "B"]
        assert all(e.type == "text" for e in events)


class TestParseEventThinking:
    def test_emits_thinking_on_delta_when_included(self):
        # Arrange — Cursor streams reasoning as thinking deltas; there is no full-block
        # carrier (completed has no text), so each non-empty delta is surfaced.
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(THINKING_DELTA_EVENT, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert "PONG" in events[0].content

    def test_skips_thinking_delta_when_not_included(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(THINKING_DELTA_EVENT, include_thinking=False)

        # Assert
        assert events == []

    def test_skips_empty_thinking_delta(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(THINKING_DELTA_EMPTY_EVENT, include_thinking=True)

        # Assert
        assert events == []

    def test_skips_thinking_completed_even_when_included(self):
        # Arrange — the completed carrier has no text.
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(THINKING_COMPLETED_EVENT, include_thinking=True)

        # Assert
        assert events == []


class TestParseEventToolUse:
    def test_emits_tool_use_with_command_on_shell_started(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(TOOL_CALL_SHELL_STARTED_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "tool_use"
        assert events[0].content == "echo hello-from-shell"

    def test_emits_tool_use_with_name_on_mcp_started(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(TOOL_CALL_MCP_STARTED_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "tool_use"
        assert events[0].content == "plugin-serena-serena-write_memory"

    def test_skips_tool_call_completed(self):
        # Arrange — one tool_use per call, on `started` only.
        adapter = CursorAdapter()

        # Act / Assert
        assert adapter._parse_event(TOOL_CALL_SHELL_COMPLETED_EVENT, include_thinking=False) == []
        assert adapter._parse_event(TOOL_CALL_MCP_REJECTED_EVENT, include_thinking=False) == []


class TestParseEventResult:
    def test_emits_ok_result_with_output_tokens_and_duration(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(RESULT_SUCCESS_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].cost == 0.0
        assert events[0].output_tokens == 46
        assert events[0].duration == 2.964

    def test_emits_error_result_when_is_error_true(self):
        # Arrange
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(RESULT_ERROR_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "error"
        assert events[0].output_tokens == 5

    def test_result_without_usage_defaults_tokens_to_zero(self):
        # Arrange — be defensive against a missing usage object.
        adapter = CursorAdapter()

        # Act
        events = adapter._parse_event(RESULT_NO_USAGE_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].content == "ok"
        assert events[0].output_tokens == 0


class TestParseEventIgnored:
    def test_ignores_user_echo_and_unknown_events(self):
        # Arrange
        adapter = CursorAdapter()
        skipped = [USER_EVENT, UNKNOWN_EVENT, {"foo": "bar"}]

        # Act / Assert
        for event in skipped:
            assert adapter._parse_event(event, include_thinking=False) == [], event
