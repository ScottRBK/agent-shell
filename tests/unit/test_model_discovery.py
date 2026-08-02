import asyncio
import json

import pytest

from agent_shell.adapters.copilot_cli_adapter import (
    _json_rpc_result,
    _read_json_rpc_message,
    _read_json_rpc_response,
)


def _frame(message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload


class TestCopilotJsonRpcParser:
    async def test_rejects_a_non_ascii_header_clearly(self):
        # Arrange
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xff: value\r\n\r\n")

        # Act / Assert
        with pytest.raises(RuntimeError, match="invalid JSON-RPC header encoding"):
            await _read_json_rpc_message(reader)

    @pytest.mark.parametrize("value", [b"not-a-number", b"-1"])
    async def test_rejects_an_invalid_content_length_clearly(self, value):
        # Arrange
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: " + value + b"\r\n\r\n")

        # Act / Assert
        with pytest.raises(RuntimeError, match="invalid Content-Length"):
            await _read_json_rpc_message(reader)

    async def test_reads_a_utf8_message_fragmented_across_chunks(self):
        # Arrange
        expected = {"jsonrpc": "2.0", "id": 2, "result": {"name": "Modèle"}}
        framed = _frame(expected)
        reader = asyncio.StreamReader()
        reader.feed_data(framed[:11])
        reading = asyncio.create_task(_read_json_rpc_message(reader))
        await asyncio.sleep(0)
        reader.feed_data(framed[11:37])
        await asyncio.sleep(0)
        reader.feed_data(framed[37:])

        # Act
        actual = await reading

        # Assert
        assert actual == expected

    async def test_skips_notifications_before_the_requested_response(self):
        # Arrange
        notification = {"jsonrpc": "2.0", "method": "status", "params": {}}
        expected = {"jsonrpc": "2.0", "id": 2, "result": {"models": []}}
        reader = asyncio.StreamReader()
        reader.feed_data(_frame(notification) + _frame(expected))

        # Act
        actual = await _read_json_rpc_response(reader, request_id=2)

        # Assert
        assert actual == expected

    async def test_rejects_a_truncated_payload_clearly(self):
        # Arrange
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 20\r\n\r\n{}")
        reader.feed_eof()

        # Act / Assert
        with pytest.raises(RuntimeError, match="truncated JSON-RPC payload"):
            await _read_json_rpc_message(reader)

    async def test_rejects_invalid_json_clearly(self):
        # Arrange
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 8\r\n\r\nnot-json")

        # Act / Assert
        with pytest.raises(RuntimeError, match="invalid JSON-RPC payload"):
            await _read_json_rpc_message(reader)

    def test_surfaces_json_rpc_errors(self):
        # Arrange
        response = {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32000, "message": "Not authenticated"},
        }

        # Act / Assert
        with pytest.raises(RuntimeError, match="Not authenticated"):
            _json_rpc_result(response)
