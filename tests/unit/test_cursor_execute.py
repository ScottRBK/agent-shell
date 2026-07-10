import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.cursor_adapter import CursorAdapter
from agent_shell.models.agent import AgentResponse

from tests.unit.cursor_fixtures import (
    SESSION_ID,
    SYSTEM_INIT_EVENT,
    ASSISTANT_TEXT_EVENT,
    RESULT_SUCCESS_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict]):
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    return _raw_process([encoded.encode("utf-8"), b""])


def _raw_process(byte_chunks: list[bytes]):
    """A mock process whose stdout yields the given raw byte chunks verbatim (no implicit
    trailing newline), so EOF-buffer and split-across-reads paths can be exercised."""
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
        adapter = CursorAdapter()
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0

    async def test_extracts_output_tokens_and_duration_from_result(self):
        # Arrange
        adapter = CursorAdapter()
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.output_tokens == 46
        assert response.duration == 2.964

    async def test_returns_session_id_from_init(self):
        # Arrange
        adapter = CursorAdapter()
        ndjson = [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.session_id == SESSION_ID

    async def test_joins_multiple_assistant_blocks_with_newline(self):
        # Arrange — a real reply often has text before and after a tool call, arriving as
        # separate assistant events. execute() joins the surfaced text blocks with "\n".
        adapter = CursorAdapter()
        text_a = {"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "A"}]}}
        text_b = {"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "B"}]}}
        ndjson = [SYSTEM_INIT_EVENT, text_a, text_b, RESULT_SUCCESS_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="x")

        # Assert
        assert response.response == "A\nB"


class TestExecuteTransportEdges:
    async def test_eof_buffer_path_surfaces_final_result(self):
        # Arrange — a real cursor-agent run's final result can arrive WITHOUT a trailing
        # newline, so it is flushed only via stream()'s EOF-buffer branch.
        adapter = CursorAdapter()
        no_newline = "\n".join(
            json.dumps(e) for e in [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT]
        )  # deliberately no trailing "\n"
        mock_process = _raw_process([no_newline.encode("utf-8"), b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.response == "PONG"
        assert response.output_tokens == 46

    async def test_eof_buffer_tolerates_malformed_trailing_bytes(self):
        # Arrange — trailing non-JSON bytes with no newline must be skipped, not raise.
        adapter = CursorAdapter()
        stream = (
            "\n".join(json.dumps(e) for e in [SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT, RESULT_SUCCESS_EVENT])
            + "\n{ this is not valid json"
        )
        mock_process = _raw_process([stream.encode("utf-8"), b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.response == "PONG"
        assert response.output_tokens == 46

    async def test_reassembles_event_split_across_reads(self):
        # Arrange — read(65536) returns at an arbitrary boundary, so a single NDJSON line can
        # span two reads; the buffer must reassemble it.
        adapter = CursorAdapter()
        encoded = (
            json.dumps(SYSTEM_INIT_EVENT) + "\n" + json.dumps(RESULT_SUCCESS_EVENT) + "\n"
        ).encode("utf-8")
        mid = len(encoded) // 2
        mock_process = _raw_process([encoded[:mid], encoded[mid:], b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.output_tokens == 46
        assert response.session_id == SESSION_ID

    async def test_no_result_degrades_gracefully(self):
        # Arrange — a killed/truncated run emits text/init but no result event.
        adapter = CursorAdapter()
        mock_process = _make_mock_process([SYSTEM_INIT_EVENT, ASSISTANT_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert — text + session_id still captured; cost/tokens fall back to zero.
        assert response.response == "PONG"
        assert response.cost == 0.0
        assert response.output_tokens == 0
        assert response.session_id == SESSION_ID
