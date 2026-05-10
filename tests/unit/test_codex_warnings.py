import json
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.adapters.codex_adapter import CodexAdapter

from tests.unit.codex_fixtures import TURN_COMPLETED_EVENT


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


async def _drain_stream(adapter, **kwargs):
    async for _ in adapter.stream(cwd="/tmp", prompt="test", **kwargs):
        pass


def _record_warnings():
    ctx = warnings.catch_warnings(record=True)
    captured = ctx.__enter__()
    warnings.simplefilter("always")
    return ctx, captured


class TestIncludeThinkingWarning:
    async def test_warns_when_include_thinking_true(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="reasoning"):
                await _drain_stream(adapter, include_thinking=True)

    async def test_no_warning_when_include_thinking_false(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, include_thinking=False)

        # Assert
        assert not any("reasoning" in str(w.message) for w in recorded)

    async def test_warns_only_once_per_instance(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process_1 = _make_mock_process([TURN_COMPLETED_EVENT])
        mock_process_2 = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_1):
                await _drain_stream(adapter, include_thinking=True)
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_2):
                await _drain_stream(adapter, include_thinking=True)

        # Assert
        thinking_warnings = [w for w in recorded if "reasoning" in str(w.message)]
        assert len(thinking_warnings) == 1


class TestAllowedToolsWarning:
    async def test_warns_when_allowed_tools_non_empty(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="allowed_tools"):
                await _drain_stream(adapter, allowed_tools=["Bash"])

    async def test_no_warning_when_allowed_tools_none(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, allowed_tools=None)

        # Assert
        assert not any("allowed_tools" in str(w.message) for w in recorded)

    async def test_no_warning_when_allowed_tools_empty(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, allowed_tools=[])

        # Assert
        assert not any("allowed_tools" in str(w.message) for w in recorded)

    async def test_warns_only_once_per_instance(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process_1 = _make_mock_process([TURN_COMPLETED_EVENT])
        mock_process_2 = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_1):
                await _drain_stream(adapter, allowed_tools=["Bash"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_2):
                await _drain_stream(adapter, allowed_tools=["Bash"])

        # Assert
        allowed_warnings = [w for w in recorded if "allowed_tools" in str(w.message)]
        assert len(allowed_warnings) == 1
