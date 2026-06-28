import json
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.pi_adapter import PiAdapter
from agent_shell.models.agent import AgentResponse

from tests.unit.pi_fixtures import (
    SESSION_EVENT,
    TEXT_END_UPDATE,
    AGENT_END_TEXT_EVENT,
    AGENT_END_TOOLUSE_EVENT,
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
        adapter = PiAdapter()
        ndjson = [SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert isinstance(response, AgentResponse)
        assert response.response == "PONG"
        assert response.cost == 0.0

    async def test_extracts_summed_output_tokens_from_agent_end(self):
        # Arrange — single-turn text run reports 27 output tokens.
        adapter = PiAdapter()
        ndjson = [SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.output_tokens == 27

    async def test_sums_output_tokens_across_tool_use_turns(self):
        # Arrange — two assistant turns (47 + 8).
        adapter = PiAdapter()
        ndjson = [SESSION_EVENT, AGENT_END_TOOLUSE_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="use bash")

        # Assert
        assert response.output_tokens == 55

    async def test_returns_session_id_from_session_event(self):
        # Arrange
        adapter = PiAdapter()
        ndjson = [SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.session_id == "019f0ae6-995e-780b-b2e7-f00d2d72873f"

    async def test_joins_multiple_text_blocks_with_newline(self):
        # Arrange — the delta-skipping design relies on text being surfaced per text_end block
        # and execute() joining blocks with "\n" (a real reply often has text before and after
        # a tool call). A "return last block" or "concat without separator" bug would corrupt this.
        adapter = PiAdapter()
        text_a = {"type": "message_update",
                  "assistantMessageEvent": {"type": "text_end", "content": "A"}}
        text_b = {"type": "message_update",
                  "assistantMessageEvent": {"type": "text_end", "content": "B"}}
        ndjson = [SESSION_EVENT, text_a, AGENT_END_TOOLUSE_EVENT, text_b, AGENT_END_TEXT_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="x")

        # Assert
        assert response.response == "A\nB"


class TestExecuteTransportEdges:
    async def test_eof_buffer_path_surfaces_final_agent_end(self):
        # Arrange — a real pi run's final agent_end often arrives WITHOUT a trailing newline,
        # so it is flushed only via stream()'s EOF-buffer branch, not the newline loop.
        adapter = PiAdapter()
        no_newline = "\n".join(
            json.dumps(e) for e in [SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT]
        )  # deliberately no trailing "\n"
        mock_process = _raw_process([no_newline.encode("utf-8"), b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.response == "PONG"
        assert response.output_tokens == 27

    async def test_eof_buffer_tolerates_malformed_trailing_bytes(self):
        # Arrange — trailing non-JSON bytes with no newline must be skipped, not raise.
        adapter = PiAdapter()
        stream = (
            "\n".join(json.dumps(e) for e in [SESSION_EVENT, TEXT_END_UPDATE, AGENT_END_TEXT_EVENT])
            + "\n{ this is not valid json"
        )
        mock_process = _raw_process([stream.encode("utf-8"), b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.response == "PONG"
        assert response.output_tokens == 27

    async def test_reassembles_event_split_across_reads(self):
        # Arrange — read(65536) returns at an arbitrary boundary, so a single NDJSON line can
        # span two reads; the buffer must reassemble it. Split at an ASCII boundary.
        adapter = PiAdapter()
        encoded = (
            json.dumps(SESSION_EVENT) + "\n" + json.dumps(AGENT_END_TEXT_EVENT) + "\n"
        ).encode("utf-8")
        mid = len(encoded) // 2
        mock_process = _raw_process([encoded[:mid], encoded[mid:], b""])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert
        assert response.output_tokens == 27
        assert response.session_id == "019f0ae6-995e-780b-b2e7-f00d2d72873f"

    async def test_no_agent_end_degrades_gracefully(self):
        # Arrange — a killed/truncated pi run emits text/session but no agent_end (no result).
        adapter = PiAdapter()
        mock_process = _make_mock_process([SESSION_EVENT, TEXT_END_UPDATE])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            response = await adapter.execute(cwd="/tmp", prompt="ping")

        # Assert — text + session_id still captured; cost/tokens fall back to zero.
        assert response.response == "PONG"
        assert response.cost == 0.0
        assert response.output_tokens == 0
        assert response.session_id == "019f0ae6-995e-780b-b2e7-f00d2d72873f"
