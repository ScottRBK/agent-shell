"""Tests that every streaming adapter owns cleanup through an exact process handle."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.process_cleanup import _guardians
from tests.unit.adapter_matrix import ADAPTERS, OK_RESULT_EVENT

MOCK_PID = 54321


def _make_mock_process(event: dict):
    chunks = [(json.dumps(event) + "\n").encode("utf-8"), b""]
    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = 0
    process.wait = AsyncMock()
    process.pid = MOCK_PID
    return process


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_stream_registers_its_exact_run_handle_with_a_guardian(adapter_cls):
    # Arrange
    adapter = adapter_cls()
    process = _make_mock_process(OK_RESULT_EVENT[adapter_cls])

    # Act
    registered_during_stream = False
    with patch("asyncio.create_subprocess_exec", return_value=process):
        async for _ in adapter.stream(cwd="/tmp", prompt="test"):
            run_handle = adapter._active_processes[0]
            if run_handle in _guardians:
                registered_during_stream = True

    # Assert
    assert registered_during_stream


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_completed_stream_releases_its_guardian(adapter_cls):
    # Arrange
    adapter = adapter_cls()
    process = _make_mock_process(OK_RESULT_EVENT[adapter_cls])

    # Act
    run_handle = None
    with patch("asyncio.create_subprocess_exec", return_value=process):
        async for _ in adapter.stream(cwd="/tmp", prompt="test"):
            run_handle = adapter._active_processes[0]

    # Assert
    assert run_handle is not None
    assert run_handle not in _guardians
    assert adapter._active_processes == []
