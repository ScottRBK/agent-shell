import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.adapters.grok_adapter import GrokAdapter
from agent_shell.models.agent import AgentExecutionError, AgentResponse

from tests.unit.grok_fixtures import (
    SESSION_ID,
    SYSTEM_INIT_EVENT,
    ASSISTANT_TEXT_EVENT,
    RESULT_SUCCESS_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict]):
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    return _raw_process([encoded.encode("utf-8"), b""])


def _raw_process(byte_chunks: list[bytes]):
    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=byte_chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = 0
    process.wait = AsyncMock()
    process.pid = 12345
    return process


class TestExecute:
    async def test_collects_text_into_response(self):
        # Arrange
        adapter = GrokAdapter()
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0359528

    async def test_extracts_tokens_duration_and_session_id(self):
        # Arrange
        adapter = GrokAdapter()
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.output_tokens == 27
        assert response.duration == 4.034
        assert response.session_id == SESSION_ID

    async def test_joins_multiple_assistant_blocks_with_newline(self):
        # Arrange — full blocks (streaming-messages-json), same join as Cursor/Claude.
        adapter = GrokAdapter()
        text_a = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "A"}]},
        }
        text_b = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "B"}]},
        }
        ndjson = [SYSTEM_INIT_EVENT, text_a, text_b, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="x")

        # Assert
        assert response.response == "A\nB"


class TestExecuteTransportEdges:
    async def test_eof_buffer_path_surfaces_final_result(self):
        # Arrange
        adapter = GrokAdapter()
        no_newline = "\n".join(
            json.dumps(e)
            for e in [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        )
        mock_process = _raw_process([no_newline.encode("utf-8"), b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.response == "PONG"
        assert response.output_tokens == 27

    async def test_no_result_raises_but_keeps_the_partial_run(self):
        # Arrange
        adapter = GrokAdapter()
        mock_process = _make_mock_process([SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(AgentExecutionError) as excinfo:
                await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        error = excinfo.value
        assert str(error) == "no result event received"
        assert error.response == "PONG"
        assert error.session_id == SESSION_ID
