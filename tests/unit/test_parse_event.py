from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.fixtures import (
    SYSTEM_EVENT,
    THINKING_EVENT,
    TEXT_EVENT,
    TOOL_USE_EVENT,
    USER_TOOL_RESULT_EVENT,
    RATE_LIMIT_EVENT,
    MULTI_CONTENT_EVENT,
    RESULT_EVENT_SUCCESS,
    RESULT_EVENT_ERROR,
    UNKNOWN_EVENT,
)


class TestParseEventText:
    def test_parses_text_content(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(TEXT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "Hey! Here's some text output."


class TestParseEventToolUse:
    def test_parses_tool_use_content(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(TOOL_USE_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "tool_use"
        assert events[0].content == "Glob"


class TestParseEventThinking:
    def test_includes_thinking_when_flag_is_true(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(THINKING_EVENT, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert events[0].content == "The user wants me to respond with some text."

    def test_excludes_thinking_when_flag_is_false(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(THINKING_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventMultiContent:
    def test_parses_all_content_items(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(MULTI_CONTENT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 2
        assert events[0].type == "text"
        assert events[0].content == "Let me check that for you."
        assert events[1].type == "tool_use"
        assert events[1].content == "Read"


class TestParseEventResult:
    def test_parses_successful_result(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(RESULT_EVENT_SUCCESS, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].cost == 0.16098
        assert events[0].duration == 10.37

    def test_parses_error_result(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(RESULT_EVENT_ERROR, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "error"
        assert events[0].cost == 0.05
        assert events[0].duration == 5.0


class TestParseEventIgnored:
    def test_ignores_system_event(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(SYSTEM_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_user_tool_result_event(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(USER_TOOL_RESULT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_rate_limit_event(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(RATE_LIMIT_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_unknown_event(self):
        # Arrange
        adapter = ClaudeCodeAdapter()

        # Act
        events = adapter._parse_event(UNKNOWN_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0
