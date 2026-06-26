from agent_shell.adapters.codex_adapter import CodexAdapter

from tests.unit.codex_fixtures import (
    THREAD_STARTED_EVENT,
    TURN_STARTED_EVENT,
    AGENT_MESSAGE_COMPLETED_EVENT,
    AGENT_MESSAGE_COMPLETED_LONG_EVENT,
    COMMAND_EXECUTION_STARTED_EVENT,
    COMMAND_EXECUTION_COMPLETED_EVENT,
    TURN_COMPLETED_EVENT,
    UNKNOWN_EVENT,
)


class TestParseEventThreadStarted:
    def test_emits_session_event_with_thread_id(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(THREAD_STARTED_EVENT)

        # Assert
        assert len(events) == 1
        assert events[0].type == "session"
        assert events[0].session_id == "019e115b-8594-7393-8ed4-bd6cf6127f2a"

    def test_skips_thread_started_without_id(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event({"type": "thread.started"})

        # Assert
        assert events == []


class TestParseEventTurnStarted:
    def test_ignores_turn_started(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(TURN_STARTED_EVENT)

        # Assert
        assert events == []


class TestParseEventAgentMessage:
    def test_emits_text_for_short_agent_message(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(AGENT_MESSAGE_COMPLETED_EVENT)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "PONG"

    def test_emits_text_for_long_agent_message(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(AGENT_MESSAGE_COMPLETED_LONG_EVENT)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert "one calm" in events[0].content
        assert "five warm" in events[0].content

    def test_skips_empty_agent_message_text(self):
        # Arrange
        adapter = CodexAdapter()
        event = {
            "type": "item.completed",
            "item": {"id": "item_x", "type": "agent_message", "text": ""},
        }

        # Act
        events = adapter._parse_event(event)

        # Assert
        assert events == []


class TestParseEventCommandExecution:
    def test_ignores_command_execution_started(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(COMMAND_EXECUTION_STARTED_EVENT)

        # Assert
        assert events == []

    def test_emits_tool_use_for_command_execution_completed(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(COMMAND_EXECUTION_COMPLETED_EVENT)

        # Assert
        assert len(events) == 1
        assert events[0].type == "tool_use"
        assert "echo hi-from-tool-call" in events[0].content


class TestParseEventTurnCompleted:
    def test_emits_result_event(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(TURN_COMPLETED_EVENT)

        # Assert — output_tokens is reported raw (22). Codex's output_tokens already includes
        # reasoning tokens (billed at the output rate); for a cost measure we keep them in.
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].cost == 0.0
        assert events[0].duration == 0.0
        assert events[0].output_tokens == 22

    def test_result_without_usage_defaults_output_tokens_to_zero(self):
        # Arrange — `codex exec` always emits usage, but be defensive against its absence.
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event({"type": "turn.completed"})

        # Assert
        assert len(events) == 1
        assert events[0].output_tokens == 0

    def test_null_usage_defaults_output_tokens_to_zero(self):
        # Arrange — a present-but-null usage must degrade to 0, not crash (D1).
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event({"type": "turn.completed", "usage": None})

        # Assert
        assert len(events) == 1
        assert events[0].output_tokens == 0

    def test_output_tokens_includes_reasoning(self):
        # Arrange — this is a cost measure: Codex bills reasoning at the output rate and folds
        # it into usage.output_tokens, so the reported figure must KEEP reasoning in (22), not
        # strip it. Guards against anyone "helpfully" subtracting reasoning_output_tokens.
        adapter = CodexAdapter()
        event = {
            "type": "turn.completed",
            "usage": {"output_tokens": 22, "reasoning_output_tokens": 14},
        }

        # Act
        events = adapter._parse_event(event)

        # Assert
        assert events[0].output_tokens == 22


class TestParseEventUnknown:
    def test_ignores_unknown_event_type(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event(UNKNOWN_EVENT)

        # Assert
        assert events == []

    def test_ignores_event_without_type(self):
        # Arrange
        adapter = CodexAdapter()

        # Act
        events = adapter._parse_event({"foo": "bar"})

        # Assert
        assert events == []
