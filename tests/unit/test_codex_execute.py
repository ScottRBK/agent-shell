import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.models.agent import AgentResponse

from tests.unit.codex_fixtures import (
    THREAD_STARTED_EVENT,
    AGENT_MESSAGE_COMPLETED_EVENT,
    TURN_COMPLETED_EVENT,
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
        adapter = CodexAdapter()
        ndjson = [THREAD_STARTED_EVENT, AGENT_MESSAGE_COMPLETED_EVENT, TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"

    async def test_extracts_output_tokens_from_turn_completed(self):
        # Arrange — codex exec emits exactly one turn.completed; its output_tokens (22) is the
        # whole-run figure, reported raw (reasoning-inclusive, per the cost intent).
        adapter = CodexAdapter()
        ndjson = [THREAD_STARTED_EVENT, AGENT_MESSAGE_COMPLETED_EVENT, TURN_COMPLETED_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.output_tokens == 22
