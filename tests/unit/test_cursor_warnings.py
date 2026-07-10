import json
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.adapters.cursor_adapter import CursorAdapter

from tests.unit.cursor_fixtures import RESULT_SUCCESS_EVENT


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


class TestDisallowedToolsWarning:
    # Cursor has NO per-call deny mechanism (tool policy lives in .cursor/cli.json only),
    # so every disallowed_tools request is unenforceable and must warn — never silently
    # dropped (a silently dropped deny is a security hole).
    async def test_warns_and_adds_no_flag(self):
        # Arrange
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="disallowed_tools"):
                await _drain_stream(adapter, disallowed_tools=["bash"])

        # Assert — nothing deny-shaped is injected into the command.
        cmd_args = mock_exec.call_args[0]
        assert "--exclude-tools" not in cmd_args
        assert not any("disabled" in str(a) for a in cmd_args)

    async def test_warning_lists_the_ignored_tools(self):
        # Arrange
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, disallowed_tools=["bash", "web_search"])

        # Assert
        msgs = [str(w.message) for w in recorded if "disallowed_tools" in str(w.message)]
        assert len(msgs) == 1
        assert "bash" in msgs[0]
        assert "web_search" in msgs[0]

    async def test_warns_every_call(self):
        # Arrange — security fail-loud: warn on EVERY call, even a reused instance + same input.
        adapter = CursorAdapter()
        mock_1 = _make_mock_process([RESULT_SUCCESS_EVENT])
        mock_2 = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_1):
                await _drain_stream(adapter, disallowed_tools=["bash"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_2):
                await _drain_stream(adapter, disallowed_tools=["bash"])

        # Assert
        deny_warnings = [w for w in recorded if "disallowed_tools" in str(w.message)]
        assert len(deny_warnings) == 2

    async def test_no_warning_when_disallowed_tools_none(self):
        # Arrange
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, disallowed_tools=None)

        # Assert
        assert not any("disallowed_tools" in str(w.message) for w in recorded)


class TestAllowedToolsWarning:
    # Cursor has no per-call allowlist flag; allowed_tools is informational -> warn-once.
    async def test_warns_when_allowed_tools_non_empty(self):
        # Arrange
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="allowed_tools"):
                await _drain_stream(adapter, allowed_tools=["bash"])

    async def test_no_warning_when_allowed_tools_none_or_empty(self):
        # Arrange — a fresh process per stream() call (a mock's read side-effect is one-shot).
        adapter = CursorAdapter()

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec",
                       return_value=_make_mock_process([RESULT_SUCCESS_EVENT])):
                await _drain_stream(adapter, allowed_tools=None)
            with patch("asyncio.create_subprocess_exec",
                       return_value=_make_mock_process([RESULT_SUCCESS_EVENT])):
                await _drain_stream(adapter, allowed_tools=[])

        # Assert
        assert not any("allowed_tools" in str(w.message) for w in recorded)

    async def test_warns_only_once_per_instance(self):
        # Arrange — informational (not a security deny), so warn-once is enough.
        adapter = CursorAdapter()
        mock_1 = _make_mock_process([RESULT_SUCCESS_EVENT])
        mock_2 = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_1):
                await _drain_stream(adapter, allowed_tools=["bash"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_2):
                await _drain_stream(adapter, allowed_tools=["bash"])

        # Assert
        allowed_warnings = [w for w in recorded if "allowed_tools" in str(w.message)]
        assert len(allowed_warnings) == 1


class TestEffortWarning:
    # Cursor has no standalone effort flag (effort is only a model bracket-override, which the
    # adapter does not inject); effort is informational -> warn-once.
    async def test_warns_when_effort_set(self):
        # Arrange
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="effort"):
                await _drain_stream(adapter, effort="high")

        # Assert — no effort-shaped flag is injected.
        assert "--effort" not in mock_exec.call_args[0]

    async def test_no_warning_when_effort_none(self):
        # Arrange
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, effort=None)

        # Assert
        assert not any("effort" in str(w.message) for w in recorded)

    async def test_warns_only_once_per_instance(self):
        # Arrange
        adapter = CursorAdapter()
        mock_1 = _make_mock_process([RESULT_SUCCESS_EVENT])
        mock_2 = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_1):
                await _drain_stream(adapter, effort="high")
            with patch("asyncio.create_subprocess_exec", return_value=mock_2):
                await _drain_stream(adapter, effort="low")

        # Assert
        effort_warnings = [w for w in recorded if "effort" in str(w.message)]
        assert len(effort_warnings) == 1


class TestIncludeThinkingHonored:
    async def test_no_warning_for_include_thinking(self):
        # Arrange — Cursor streams reasoning as thinking deltas, so include_thinking is
        # honoured (not warned, unlike Codex).
        adapter = CursorAdapter()
        mock_process = _make_mock_process([RESULT_SUCCESS_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, include_thinking=True)

        # Assert
        assert recorded == []
