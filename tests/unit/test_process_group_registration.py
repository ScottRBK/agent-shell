"""Tests that adapters register/unregister process groups with the cleanup registry."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.process_cleanup import _active_process_groups

from tests.unit.fixtures import SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS
from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT as OC_TEXT_EVENT,
    STEP_FINISH_STOP_EVENT,
)

MOCK_PID = 54321


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0):
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = MOCK_PID
    return process


class TestClaudeCodeAdapterRegistration:
    async def test_registers_pid_as_pgid_on_subprocess_creation(self):
        # Arrange — setsid makes pgid == pid, so adapter registers process.pid
        _active_process_groups.clear()
        adapter = ClaudeCodeAdapter()
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            registered_during_stream = False
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                if MOCK_PID in _active_process_groups:
                    registered_during_stream = True

        # Assert
        assert registered_during_stream, "process.pid should be registered while stream is active"

        # Cleanup
        _active_process_groups.clear()

    async def test_unregisters_pid_on_normal_completion(self):
        # Arrange
        _active_process_groups.clear()
        adapter = ClaudeCodeAdapter()
        ndjson = [SYSTEM_EVENT, TEXT_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        assert MOCK_PID not in _active_process_groups, \
            "process.pid should be unregistered after normal completion"

    async def test_cancel_unregisters_pgid(self):
        # Arrange — cancel() uses os.getpgid to get the current pgid
        _active_process_groups.clear()
        adapter = ClaudeCodeAdapter()
        mock_process = AsyncMock()
        mock_process.pid = MOCK_PID
        adapter._active_processes = [mock_process]
        _active_process_groups.add(MOCK_PID)

        # Act
        with patch("os.getpgid", return_value=MOCK_PID), \
             patch("os.killpg"):
            await adapter.cancel()

        # Assert
        assert MOCK_PID not in _active_process_groups, \
            "PGID should be unregistered after cancel()"


class TestOpenCodeAdapterRegistration:
    async def test_registers_pid_as_pgid_on_subprocess_creation(self):
        # Arrange
        _active_process_groups.clear()
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, OC_TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            registered_during_stream = False
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                if MOCK_PID in _active_process_groups:
                    registered_during_stream = True

        # Assert
        assert registered_during_stream, "process.pid should be registered while stream is active"

        # Cleanup
        _active_process_groups.clear()

    async def test_unregisters_pid_on_normal_completion(self):
        # Arrange
        _active_process_groups.clear()
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, OC_TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        assert MOCK_PID not in _active_process_groups, \
            "process.pid should be unregistered after normal completion"

    async def test_cancel_unregisters_pgid(self):
        # Arrange
        _active_process_groups.clear()
        adapter = OpenCodeAdapter()
        mock_process = AsyncMock()
        mock_process.pid = MOCK_PID
        adapter._active_processes = [mock_process]
        _active_process_groups.add(MOCK_PID)

        # Act
        with patch("os.getpgid", return_value=MOCK_PID), \
             patch("os.killpg"):
            await adapter.cancel()

        # Assert
        assert MOCK_PID not in _active_process_groups, \
            "PGID should be unregistered after cancel()"
