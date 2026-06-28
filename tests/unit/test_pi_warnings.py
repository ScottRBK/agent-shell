import json
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.adapters.pi_adapter import PiAdapter

from tests.unit.pi_fixtures import AGENT_END_TEXT_EVENT


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


class TestDisallowedToolsNative:
    async def test_bash_appends_exclude_tools(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=["bash"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--exclude-tools" in cmd_args
        assert cmd_args[cmd_args.index("--exclude-tools") + 1] == "bash"

    async def test_edit_fans_out_to_edit_and_write(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=["edit"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--exclude-tools") + 1] == "edit,write"

    async def test_read_maps_to_read(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=["read"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--exclude-tools") + 1] == "read"

    async def test_non_canonical_name_passes_through(self):
        # Arrange — a named extension tool is denied verbatim, no warning.
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, disallowed_tools=["my_custom_tool"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--exclude-tools") + 1] == "my_custom_tool"
        assert recorded == []

    async def test_no_exclude_flag_when_disallowed_none(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=None)

        # Assert
        assert "--exclude-tools" not in mock_exec.call_args[0]


class TestUnsupportedDenyWarns:
    async def test_web_search_warns_and_adds_no_flag(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="web_search"):
                await _drain_stream(adapter, disallowed_tools=["web_search"])

        assert "--exclude-tools" not in mock_exec.call_args[0]

    async def test_web_fetch_warns(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="web_fetch"):
                await _drain_stream(adapter, disallowed_tools=["web_fetch"])

    async def test_mixed_denies_bash_and_warns_web_search(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="web_search"):
                await _drain_stream(adapter, disallowed_tools=["web_search", "bash"])

        # Assert — bash still denied even while warning about web_search.
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--exclude-tools") + 1] == "bash"

    async def test_warns_every_call_for_unsupported_deny(self):
        # Arrange — security fail-loud: warn on EVERY call, even reused instance + same input.
        adapter = PiAdapter()
        mock_1 = _make_mock_process([AGENT_END_TEXT_EVENT])
        mock_2 = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_1):
                await _drain_stream(adapter, disallowed_tools=["web_search"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_2):
                await _drain_stream(adapter, disallowed_tools=["web_search"])

        # Assert
        deny_warnings = [w for w in recorded if "web_search" in str(w.message)]
        assert len(deny_warnings) == 2

    async def test_later_different_unsupported_deny_still_warns(self):
        # Arrange — a NEW unenforceable deny must not be silently dropped after an earlier one.
        adapter = PiAdapter()
        mock_1 = _make_mock_process([AGENT_END_TEXT_EVENT])
        mock_2 = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_1):
                await _drain_stream(adapter, disallowed_tools=["web_search"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_2):
                await _drain_stream(adapter, disallowed_tools=["web_fetch"])

        # Assert
        msgs = [str(w.message) for w in recorded if "ignoring" in str(w.message)]
        assert len(msgs) == 2
        assert any("web_search" in m for m in msgs)
        assert any("web_fetch" in m for m in msgs)


class TestNoSpuriousWarnings:
    async def test_no_warning_for_allowed_tools(self):
        # Arrange — Pi supports an allowlist (--tools), so no warn (unlike Codex).
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, allowed_tools=["bash", "read"])

        # Assert
        assert recorded == []
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--tools") + 1] == "bash,read"

    async def test_no_warning_for_effort(self):
        # Arrange — effort maps to --thinking, a real Pi flag.
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, effort="high")

        # Assert
        assert recorded == []
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[cmd_args.index("--thinking") + 1] == "high"

    async def test_no_warning_for_include_thinking(self):
        # Arrange — Pi streams thinking; include_thinking is honoured, not warned.
        adapter = PiAdapter()
        mock_process = _make_mock_process([AGENT_END_TEXT_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, include_thinking=True)

        # Assert
        assert recorded == []
