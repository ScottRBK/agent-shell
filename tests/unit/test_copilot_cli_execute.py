import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.models.agent import AgentResponse, StreamEvent

from tests.unit.copilot_fixtures import (
    TURN_START_EVENT,
    MESSAGE_DELTA_EVENT,
    MESSAGE_DELTA_EVENT_2,
    MESSAGE_DELTA_EVENT_3,
    MESSAGE_EVENT_NO_TOOLS,
    RESULT_EVENT_SUCCESS,
    make_assistant_message,
)


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0, stderr: bytes = b""):
    """Create a mock subprocess that yields NDJSON lines from stdout."""
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = 12345
    return process


class TestExecute:
    async def test_returns_response_with_text_and_session_id(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [
            TURN_START_EVENT,
            MESSAGE_DELTA_EVENT,
            MESSAGE_DELTA_EVENT_2,
            MESSAGE_DELTA_EVENT_3,
            MESSAGE_EVENT_NO_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "HEL\nLO\n_WORLD"
        assert response.session_id == "01036873-9931-4e3e-b3cb-14793ae370f9"

    async def test_returns_response_with_empty_cost(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [
            MESSAGE_DELTA_EVENT,
            MESSAGE_EVENT_NO_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.cost == 0.0

    async def test_accumulates_output_tokens_across_messages(self):
        # Arrange — Copilot's result event has NO token fields; output tokens live only on each
        # assistant.message (per message, not cumulative), so a multi-message run must sum them.
        adapter = CopilotCLIAdapter()
        ndjson = [
            make_assistant_message(618),
            make_assistant_message(71),
            make_assistant_message(201),
            make_assistant_message(36),
            RESULT_EVENT_SUCCESS,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert — 618 + 71 + 201 + 36, NOT 36 (take-last) and NOT 0 (result has no tokens).
        assert response.output_tokens == 926

    async def test_single_message_output_tokens_is_that_message(self):
        # Arrange — MESSAGE_EVENT_NO_TOOLS carries data.outputTokens == 35.
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_EVENT_NO_TOOLS, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.output_tokens == 35

    async def test_output_tokens_reset_between_runs_on_reused_adapter(self):
        # Arrange — accumulator must be local to each stream() call; a leak would make the
        # second run's count include the first run's tokens.
        adapter = CopilotCLIAdapter()
        run_one = _make_mock_process([
            make_assistant_message(618), make_assistant_message(36), RESULT_EVENT_SUCCESS,
        ])
        run_two = _make_mock_process([
            make_assistant_message(71), RESULT_EVENT_SUCCESS,
        ])

        # Act — two execute() calls on the SAME adapter instance.
        with patch("asyncio.create_subprocess_exec", side_effect=[run_one, run_two]):
            first = await adapter.execute(cwd="/tmp", prompt="one")
            second = await adapter.execute(cwd="/tmp", prompt="two")

        # Assert — second run is independent (71), not 71 + the first run's 654.
        assert first.output_tokens == 654
        assert second.output_tokens == 71

    async def test_aggregates_text_from_multiple_deltas(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [
            MESSAGE_DELTA_EVENT,
            MESSAGE_DELTA_EVENT_2,
            MESSAGE_DELTA_EVENT_3,
            MESSAGE_EVENT_NO_TOOLS,
            RESULT_EVENT_SUCCESS,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.response == "HEL\nLO\n_WORLD"
