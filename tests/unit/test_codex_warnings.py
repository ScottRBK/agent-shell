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


class TestDisallowedToolsWebSearch:
    async def test_web_search_appends_quoted_config_flag(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=["web_search"])

        # Assert — consecutive `-c web_search="disabled"` (TOML string mode).
        cmd_args = mock_exec.call_args[0]
        assert 'web_search="disabled"' in cmd_args
        idx = cmd_args.index('web_search="disabled"')
        assert cmd_args[idx - 1] == "-c"

    async def test_web_search_emits_no_warning(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, disallowed_tools=["web_search"])

        # Assert
        assert not any("web_search" in str(w.message) for w in recorded)

    async def test_unsupported_name_warns_and_no_config_flag(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="web_search"):
                await _drain_stream(adapter, disallowed_tools=["bash"])

        cmd_args = mock_exec.call_args[0]
        assert 'web_search="disabled"' not in cmd_args

    async def test_mixed_denies_web_search_and_warns_rest(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="bash"):
                await _drain_stream(adapter, disallowed_tools=["web_search", "bash"])

        # Assert — web_search still disabled even while warning about bash.
        cmd_args = mock_exec.call_args[0]
        assert 'web_search="disabled"' in cmd_args

    async def test_no_config_flag_when_disallowed_tools_none(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(adapter, disallowed_tools=None)

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert 'web_search="disabled"' not in cmd_args

    async def test_warns_every_call_for_unsupported_deny(self):
        # Arrange — security fail-loud: each call with an unenforceable deny must warn,
        # even on a reused instance with identical input (unlike the informational
        # allowed_tools/include_thinking warn-once flags).
        adapter = CodexAdapter()
        mock_process_1 = _make_mock_process([TURN_COMPLETED_EVENT])
        mock_process_2 = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_1):
                await _drain_stream(adapter, disallowed_tools=["bash"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_2):
                await _drain_stream(adapter, disallowed_tools=["bash"])

        # Assert
        denial_warnings = [w for w in recorded if "Codex can only deny" in str(w.message)]
        assert len(denial_warnings) == 2

    async def test_later_different_unsupported_deny_still_warns(self):
        # Arrange — the core fix: a NEW unenforceable deny on a reused instance must not be
        # silently dropped just because an earlier call already warned about a different tool.
        adapter = CodexAdapter()
        mock_process_1 = _make_mock_process([TURN_COMPLETED_EVENT])
        mock_process_2 = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_1):
                await _drain_stream(adapter, disallowed_tools=["bash"])
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_2):
                await _drain_stream(adapter, disallowed_tools=["edit"])

        # Assert
        msgs = [str(w.message) for w in recorded if "Codex can only deny" in str(w.message)]
        assert len(msgs) == 2
        assert any("bash" in m for m in msgs)
        assert any("edit" in m for m in msgs)


class TestDisallowedToolsResume:
    async def test_web_search_disabled_on_resume_branch(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await _drain_stream(
                adapter, disallowed_tools=["web_search"], session_id="thread_123"
            )

        # Assert — resume branch also carries the deny, prompt stays last.
        cmd_args = mock_exec.call_args[0]
        assert "resume" in cmd_args
        assert 'web_search="disabled"' in cmd_args
        idx = cmd_args.index('web_search="disabled"')
        assert cmd_args[idx - 1] == "-c"


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


class TestDisallowedToolsWarningContent:
    async def test_warning_lists_exact_unsupported_names_sorted(self):
        # Arrange — guards the set-math sorted(set(disallowed) - {"web_search"}).
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(
                    adapter, disallowed_tools=["read", "bash", "web_search"]
                )

        # Assert — web_search removed (it IS deniable), the rest reported sorted.
        msgs = [str(w.message) for w in recorded if "Codex can only deny" in str(w.message)]
        assert len(msgs) == 1
        assert "['bash', 'read']" in msgs[0]


class TestWebSearchMinimalEffortFailOpen:
    # Codex IGNORES web_search="disabled" under model_reasoning_effort="minimal"
    # (openai/codex#5002), so the only Codex-enforceable deny silently fails OPEN at that effort.
    # The adapter must surface that — fail-loud, every call — rather than emit a no-op deny silently.
    async def test_warns_when_web_search_denied_under_minimal_effort(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.warns(UserWarning, match="minimal"):
                await _drain_stream(
                    adapter, disallowed_tools=["web_search"], effort="minimal"
                )

    async def test_no_minimal_warning_at_other_efforts(self):
        # Arrange — the deny IS enforced at non-minimal efforts, so no fail-open warning.
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(
                    adapter, disallowed_tools=["web_search"], effort="high"
                )

        # Assert
        assert not any("minimal" in str(w.message) for w in recorded)

    async def test_no_minimal_warning_without_web_search_deny(self):
        # Arrange — minimal effort alone is fine; only the web_search deny + minimal combo fails open.
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                await _drain_stream(adapter, effort="minimal")

        # Assert
        assert not any("minimal" in str(w.message) for w in recorded)

    async def test_warns_every_call_under_minimal_effort(self):
        # Arrange — a silently dropped deny is a security hole, so warn on EVERY call (not warn-once).
        adapter = CodexAdapter()
        mock_process_1 = _make_mock_process([TURN_COMPLETED_EVENT])
        mock_process_2 = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_1):
                await _drain_stream(
                    adapter, disallowed_tools=["web_search"], effort="minimal"
                )
            with patch("asyncio.create_subprocess_exec", return_value=mock_process_2):
                await _drain_stream(
                    adapter, disallowed_tools=["web_search"], effort="minimal"
                )

        # Assert
        minimal_warnings = [w for w in recorded if "minimal" in str(w.message)]
        assert len(minimal_warnings) == 2


class TestExecuteForwardsDisallowedTools:
    async def test_execute_forwards_web_search_deny_to_command(self):
        # Arrange
        adapter = CodexAdapter()
        mock_process = _make_mock_process([TURN_COMPLETED_EVENT])

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.execute(cwd="/tmp", prompt="test", disallowed_tools=["web_search"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert 'web_search="disabled"' in cmd_args
