from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.copilot_fixtures import (
    MCP_SERVER_STATUS_EVENT,
    MCP_SERVERS_LOADED_EVENT,
    TOOLS_UPDATED_EVENT,
    BACKGROUND_TASKS_CHANGED_EVENT,
    USER_MESSAGE_EVENT,
    TURN_START_EVENT,
    REASONING_DELTA_EVENT,
    REASONING_EVENT,
    MESSAGE_DELTA_EVENT,
    MESSAGE_EVENT_NO_TOOLS,
    MESSAGE_EVENT_WITH_TOOLS,
    TOOL_EXEC_START_EVENT,
    TOOL_EXEC_COMPLETE_EVENT,
    TURN_END_EVENT,
    RESULT_EVENT_SUCCESS,
    RESULT_EVENT_ERROR,
    UNKNOWN_EVENT,
)


class TestParseEventSessionEvents:
    def test_ignores_mcp_server_status_changed(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(MCP_SERVER_STATUS_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_mcp_servers_loaded(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(MCP_SERVERS_LOADED_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_tools_updated(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(TOOLS_UPDATED_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_background_tasks_changed(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(BACKGROUND_TASKS_CHANGED_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventUserMessage:
    def test_ignores_user_message_event(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(USER_MESSAGE_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventTurnStart:
    def test_ignores_turn_start_no_event_emitted(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(TURN_START_EVENT, include_thinking=False)

        # Assert
        assert events == []


class TestParseEventReasoning:
    def test_includes_reasoning_delta_when_flag_true(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_DELTA_EVENT, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert events[0].content == "The user wants me to return exactly 'HELLO_WORLD'."

    def test_excludes_reasoning_delta_when_flag_false(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_DELTA_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_includes_reasoning_block_when_flag_true(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_EVENT, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert "**Planning file listing task**" in events[0].content

    def test_excludes_reasoning_block_when_flag_false(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventMessageDelta:
    def test_emits_text_from_delta(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(MESSAGE_DELTA_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].content == "HEL"


class TestParseEventMessageNoTools:
    def test_ignores_message_without_tool_requests(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(MESSAGE_EVENT_NO_TOOLS, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventMessageWithTools:
    def test_emits_tool_use_for_each_request(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(MESSAGE_EVENT_WITH_TOOLS, include_thinking=False)

        # Assert
        assert len(events) == 2
        assert events[0].type == "tool_use"
        assert events[0].content == "report_intent"
        assert events[1].type == "tool_use"
        assert events[1].content == "bash"


class TestParseEventToolExecution:
    def test_ignores_tool_execution_start(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(TOOL_EXEC_START_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_ignores_tool_execution_complete(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(TOOL_EXEC_COMPLETE_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventTurnEnd:
    def test_ignores_turn_end(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(TURN_END_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventResult:
    def test_parses_successful_result(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(RESULT_EVENT_SUCCESS, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "ok"
        assert events[0].cost == 0.0
        assert events[0].duration == 1.138
        assert events[0].session_id == "01036873-9931-4e3e-b3cb-14793ae370f9"

    def test_parses_error_result(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(RESULT_EVENT_ERROR, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].type == "result"
        assert events[0].content == "error"
        assert events[0].cost == 0.0
        assert events[0].duration == 5.0
        assert events[0].session_id == "01036873-9931-4e3e-b3cb-14793ae370f9"

    def test_result_without_usage_defaults_duration_to_zero(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        event = {
            "type": "result",
            "timestamp": "2026-04-18T23:14:51.605Z",
            "sessionId": "test-session",
            "exitCode": 0,
            "usage": {},
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].duration == 0.0
        assert events[0].session_id == "test-session"

    def test_result_without_session_id(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        event = {
            "type": "result",
            "timestamp": "2026-04-18T23:14:51.605Z",
            "exitCode": 0,
            "usage": {},
        }

        # Act
        events = adapter._parse_event(event, include_thinking=False)

        # Assert
        assert len(events) == 1
        assert events[0].session_id is None


class TestParseEventUnknown:
    def test_ignores_unknown_event(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(UNKNOWN_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0


class TestParseEventReasoningDisabled:
    def test_reasoning_delta_ignored_when_include_thinking_false(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_DELTA_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_reasoning_block_ignored_when_include_thinking_false(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_EVENT, include_thinking=False)

        # Assert
        assert len(events) == 0

    def test_reasoning_delta_emitted_when_include_thinking_true(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_DELTA_EVENT, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert "HELLO_WORLD" in events[0].content

    def test_reasoning_block_emitted_when_include_thinking_true(self):
        # Arrange
        adapter = CopilotCLIAdapter()

        # Act
        events = adapter._parse_event(REASONING_EVENT, include_thinking=True)

        # Assert
        assert len(events) == 1
        assert events[0].type == "thinking"
        assert "Planning" in events[0].content
