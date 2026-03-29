from agent_shell.adapters.opencode_adapter import OpenCodeAdapter

from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT,
    TOOL_USE_EVENT,
    STEP_FINISH_STOP_EVENT,
    STEP_FINISH_TOOL_CALLS_EVENT,
    ERROR_EVENT,
    UNKNOWN_EVENT,
)


class TestParseEventStepStart:
    def test_parses_session_id_from_step_start(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(STEP_START_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "system"
        assert events[0].session_id == "test-session"

    def test_step_start_has_empty_content(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(STEP_START_EVENT, include_thinking=False)

        # Assert
        assert events[0].content == ""


class TestParseEventText:
    def test_parses_text_content(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(TEXT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "hello world"


class TestParseEventToolUse:
    def test_parses_tool_name(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(TOOL_USE_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "tool_use"
        assert events[0].content == "bash"


class TestParseEventStepFinish:
    def test_parses_final_result_with_stop_reason(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(STEP_FINISH_STOP_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].cost == 0.05
        assert events[0].session_id == "test-session"

    def test_ignores_intermediate_step_finish_with_tool_calls_reason(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(STEP_FINISH_TOOL_CALLS_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_result_without_cost_defaults_to_zero(self):
        # Arrange
        adapter = OpenCodeAdapter()
        event = {
            "type": "step_finish",
            "timestamp": 1774816328736,
            "sessionID": "test-session",
            "part": {
                "reason": "stop",
                "type": "step-finish",
                "tokens": {"total": 100, "input": 50, "output": 50, "reasoning": 0, "cache": {"write": 0, "read": 0}},
            },
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert events[0].cost == 0.0


class TestParseEventError:
    def test_parses_error_message(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(ERROR_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "error"
        assert "Model not found" in events[0].content


class TestParseEventIgnored:
    def test_ignores_unknown_event_type(self):
        # Arrange
        adapter = OpenCodeAdapter()

        # Act
        events = adapter._parse_event(UNKNOWN_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0
