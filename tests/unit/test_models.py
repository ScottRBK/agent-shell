"""Model-level tests for the shared StreamEvent / AgentResponse dataclasses."""

from agent_shell.models.agent import AgentResponse, StreamEvent


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
