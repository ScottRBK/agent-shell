from agent_shell.adapters.grok_adapter import GrokAdapter

from tests.unit.grok_fixtures import (
    SESSION_ID,
    SYSTEM_INIT_EVENT,
    SYSTEM_INIT_NO_SESSION_EVENT,
    USER_EVENT,
    ASSISTANT_TEXT_EVENT,
    ASSISTANT_TEXT_EMPTY_EVENT,
    ASSISTANT_THINKING_AND_TEXT_EVENT,
    ASSISTANT_TOOL_USE_EVENT,
    ASSISTANT_SERVER_TOOL_USE_EVENT,
    RESULT_SUCCESS_EVENT,
    RESULT_ERROR_EVENT,
    RESULT_NO_USAGE_EVENT,
    UNKNOWN_EVENT,
)


class TestParseEventSession:
    def test_emits_system_event_with_session_id(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(SYSTEM_INIT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "system"
        assert events[0].session_id == SESSION_ID

    def test_skips_init_without_session_id(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(SYSTEM_INIT_NO_SESSION_EVENT, include_thinking=False)

        # Assert
        assert events == []


class TestParseEventText:
    def test_emits_text_on_assistant_block(self):
        # Arrange — full text blocks, not token deltas (streaming-messages-json).
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(ASSISTANT_TEXT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "PONG"

    def test_skips_empty_assistant_text(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(ASSISTANT_TEXT_EMPTY_EVENT, include_thinking=False)

        # Assert
        assert events == []

    def test_emits_text_per_block_for_multi_block_message(self):
        # Arrange
        adapter = GrokAdapter()
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
    def test_emits_thinking_when_included(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(
            ASSISTANT_THINKING_AND_TEXT_EVENT, include_thinking=True,
        )

        # Assert
        thinking = [e for e in events if e.type == "thinking"]
        text = [e for e in events if e.type == "text"]
        assert len(thinking) == 1
        assert "PONG" in thinking[0].content
        assert [e.content for e in text] == ["PONG"]

    def test_skips_thinking_when_not_included(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(
            ASSISTANT_THINKING_AND_TEXT_EVENT, include_thinking=False,
        )

        # Assert
        assert [e for e in events if e.type == "thinking"] == []
        assert [e.content for e in events if e.type == "text"] == ["PONG"]


class TestParseEventToolUse:
    def test_emits_tool_use_from_assistant_block(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(ASSISTANT_TOOL_USE_EVENT, include_thinking=False)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "list_dir"

    def test_emits_tool_use_from_server_tool_use_block(self):
        # Arrange — inline web search uses server_tool_use, not client tool_use.
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(
            ASSISTANT_SERVER_TOOL_USE_EVENT, include_thinking=False,
        )

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "web_search"


class TestParseEventResult:
    def test_emits_ok_result_with_cost_duration_and_raw_output_tokens(self):
        # Arrange — reasoning_tokens is a subset of output_tokens; do not add.
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(RESULT_SUCCESS_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].session_id == SESSION_ID
        assert events[0].cost == 0.0359528
        assert events[0].duration == 4.034
        assert events[0].output_tokens == 27

    def test_emits_error_result_with_structured_reason(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(RESULT_ERROR_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "error"
        assert events[0].output_tokens == 5
        assert events[0].error == "model overloaded"

    def test_result_without_usage_defaults_tokens_to_zero(self):
        # Arrange
        adapter = GrokAdapter()

        # Act
        events = adapter._parse_event(RESULT_NO_USAGE_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].content == "ok"
        assert events[0].output_tokens == 0
        assert events[0].cost == 0.0


class TestParseEventIgnored:
    def test_ignores_user_echo_and_unknown_events(self):
        # Arrange
        adapter = GrokAdapter()
        skipped = [USER_EVENT, UNKNOWN_EVENT, {"foo": "bar"}]

        # Act / Assert
        for event in skipped:
            assert adapter._parse_event(event, include_thinking=False) == [], event
