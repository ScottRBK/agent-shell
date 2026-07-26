"""Model-level tests for the shared StreamEvent / AgentResponse dataclasses."""

import pytest

from agent_shell.models.agent import AgentExecutionError, AgentResponse, StreamEvent


class TestOutputTokensDefaults:
    def test_stream_event_defaults_output_tokens_to_zero(self):
        # Arrange / Act
        event = StreamEvent(type="result", content="ok")

        # Assert
        assert event.output_tokens == 0

    def test_stream_event_accepts_explicit_output_tokens(self):
        # Arrange / Act
        event = StreamEvent(type="result", content="ok", output_tokens=565)

        # Assert
        assert event.output_tokens == 565

    def test_agent_response_defaults_output_tokens_to_zero(self):
        # Arrange / Act
        response = AgentResponse(response="hi", cost=0.0)

        # Assert
        assert response.output_tokens == 0

    def test_agent_response_accepts_explicit_output_tokens(self):
        # Arrange / Act
        response = AgentResponse(response="hi", cost=0.0, output_tokens=926)

        # Assert
        assert response.output_tokens == 926


class TestErrorDefaults:
    def test_stream_event_defaults_error_to_none(self):
        # Arrange / Act
        event = StreamEvent(type="result", content="ok")

        # Assert
        assert event.error is None

    def test_stream_event_accepts_explicit_error(self):
        # Arrange / Act
        event = StreamEvent(
            type="result", content="error",
            error="500 model name=qwen3.6-27b-8Q failed to load",
        )

        # Assert
        assert event.error == "500 model name=qwen3.6-27b-8Q failed to load"


class TestAgentExecutionError:
    """execute() raises this instead of returning a success-shaped response (issue #11)."""

    def test_str_is_the_reason_alone(self):
        # Arrange / Act — a consumer that only logs the exception must still see the cause.
        error = AgentExecutionError("500 model name=qwen3.6-27b-8Q failed to load")

        # Assert
        assert str(error) == "500 model name=qwen3.6-27b-8Q failed to load"
        assert error.reason == "500 model name=qwen3.6-27b-8Q failed to load"

    def test_carries_the_partial_run_data(self):
        # Arrange / Act — everything the old return value carried survives the raise, so a
        # failure destroys nothing the caller already paid for.
        error = AgentExecutionError(
            "provider unreachable", response="half an answer", cost=0.25,
            session_id="sess-1", duration=1.5, output_tokens=42,
        )

        # Assert
        assert error.response == "half an answer"
        assert error.cost == 0.25
        assert error.session_id == "sess-1"
        assert error.duration == 1.5
        assert error.output_tokens == 42

    def test_partial_run_data_defaults_to_empty(self):
        # Arrange / Act — a failure before any output still constructs.
        error = AgentExecutionError("no result event received")

        # Assert
        assert error.response == ""
        assert error.cost == 0.0
        assert error.session_id is None
        assert error.duration == 0.0
        assert error.output_tokens == 0

    def test_is_catchable_as_an_exception(self):
        # Arrange / Act / Assert
        with pytest.raises(AgentExecutionError):
            raise AgentExecutionError("boom")
