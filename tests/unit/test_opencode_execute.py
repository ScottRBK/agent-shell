import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.models.agent import AgentResponse

from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT,
    STEP_FINISH_STOP_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict]):
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = 0
    process.wait = AsyncMock()
    process.pid = 12345
    return process


class TestExecute:
    async def test_collects_text_into_response(self):
        # Arrange
        adapter = OpenCodeAdapter()
        second_text = {
            "type": "text",
            "timestamp": 1774816303500,
            "sessionID": "test-session",
            "part": {
                "id": "prt_second",
                "messageID": "msg_abc123",
                "sessionID": "test-session",
                "type": "text",
                "text": "And some more text.",
                "time": {"start": 1774816303500, "end": 1774816303500},
            },
        }
        ndjson = [STEP_START_EVENT, TEXT_EVENT, second_text, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert isinstance(response, AgentResponse)
        assert "hello world" in response.response
        assert "And some more text." in response.response

    async def test_extracts_cost_from_result(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.cost == 0.05

    async def test_extracts_session_id(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.session_id == "test-session"
