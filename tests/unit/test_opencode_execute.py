import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.models.agent import AgentResponse

from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT,
    TOOL_USE_EVENT,
    STEP_FINISH_STOP_EVENT,
    make_step_finish,
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

    async def test_accumulates_output_tokens_across_steps(self):
        # Arrange — the full execute() path must sum per-step outputs (286 + 193 + 42).
        adapter = OpenCodeAdapter()
        ndjson = [
            STEP_START_EVENT,
            TOOL_USE_EVENT,
            make_step_finish(286, "tool-calls"),
            STEP_START_EVENT,
            TOOL_USE_EVENT,
            make_step_finish(193, "tool-calls"),
            STEP_START_EVENT,
            TEXT_EVENT,
            make_step_finish(42, "stop"),
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.output_tokens == 521

    async def test_accumulates_output_plus_reasoning_across_steps(self):
        # Arrange — the execute() path must sum (output + reasoning) per step, since OpenCode
        # reports reasoning in a sibling field excluded from tokens.output. Reasoning is billed
        # at the output rate, so the cost measure includes it: 286+14 + 193+30 + 42+8 == 573.
        adapter = OpenCodeAdapter()
        ndjson = [
            STEP_START_EVENT,
            TOOL_USE_EVENT,
            make_step_finish(286, "tool-calls", reasoning=14),
            STEP_START_EVENT,
            TOOL_USE_EVENT,
            make_step_finish(193, "tool-calls", reasoning=30),
            STEP_START_EVENT,
            TEXT_EVENT,
            make_step_finish(42, "stop", reasoning=8),
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="test")

        # Assert
        assert response.output_tokens == 573

    async def test_output_tokens_reset_between_runs_on_reused_adapter(self):
        # Arrange — adapters are long-lived and reused; the accumulator MUST be local to each
        # stream() call. A leak would make the second run's count include the first run's tokens.
        adapter = OpenCodeAdapter()
        run_one = _make_mock_process([
            STEP_START_EVENT, TOOL_USE_EVENT, make_step_finish(286, "tool-calls"),
            STEP_START_EVENT, TEXT_EVENT, make_step_finish(42, "stop"),
        ])
        run_two = _make_mock_process([
            STEP_START_EVENT, TEXT_EVENT, make_step_finish(193, "stop"),
        ])

        # Act — two execute() calls on the SAME adapter instance.
        with patch("asyncio.create_subprocess_exec", side_effect=[run_one, run_two]):
            first = await adapter.execute(cwd="/tmp", prompt="one")
            second = await adapter.execute(cwd="/tmp", prompt="two")

        # Assert — second run is independent (193), not 193 + the first run's 328.
        assert first.output_tokens == 328
        assert second.output_tokens == 193
