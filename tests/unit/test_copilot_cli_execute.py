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
