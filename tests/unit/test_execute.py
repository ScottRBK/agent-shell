import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.models.agent import AgentResponse

from tests.unit.fixtures import (
    TEXT_EVENT,
    TOOL_USE_EVENT,
    RESULT_EVENT_SUCCESS,
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
        adapter = ClaudeCodeAdapter()
        second_text = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "And some more text."}]},
        }
        ndjson = [TEXT_EVENT, second_text, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert isinstance(response, AgentResponse)
        assert "Hey! Here's some text output." in response.response
        assert "And some more text." in response.response

    async def test_extracts_cost_from_result(self):
        # Arrange
        adapter = ClaudeCodeAdapter()
        ndjson = [TEXT_EVENT, TOOL_USE_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.cost == 0.16098
