import json
import os
from unittest.mock import AsyncMock, patch, MagicMock

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter

from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT,
    STEP_FINISH_STOP_EVENT,
)


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0, stderr: bytes = b""):
    """Create a mock subprocess that yields NDJSON lines from stdout."""
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = 12345
    return process


async def _run_stream(adapter, mock_exec, **kwargs):
    mock_process = _make_mock_process([STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT])
    mock_exec.return_value = mock_process
    async for _ in adapter.stream(cwd="/tmp", prompt="test", **kwargs):
        pass
    return mock_exec.call_args


class TestSpawnArguments:
    async def test_auto_approve_passes_skip_permissions_flag(self):
        adapter = OpenCodeAdapter()
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            call = await _run_stream(adapter, mock_exec, auto_approve=True)
        assert "--dangerously-skip-permissions" in call.args

    async def test_no_auto_approve_omits_skip_permissions_flag(self):
        adapter = OpenCodeAdapter()
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            call = await _run_stream(adapter, mock_exec, auto_approve=False)
        assert "--dangerously-skip-permissions" not in call.args

    async def test_env_pwd_pinned_to_cwd(self):
        """opencode resolves the project dir from $PWD; a stale inherited PWD
        (the launcher's directory) must not leak into the child process."""
        adapter = OpenCodeAdapter()
        with patch.dict(os.environ, {"PWD": "/somewhere/stale"}), \
                patch("asyncio.create_subprocess_exec") as mock_exec:
            call = await _run_stream(adapter, mock_exec)
        env = call.kwargs["env"]
        assert env["PWD"] == os.path.abspath("/tmp")
        # rest of the parent environment still flows through
        assert "PATH" in env

    async def test_cwd_still_applied(self):
        adapter = OpenCodeAdapter()
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            call = await _run_stream(adapter, mock_exec)
        assert call.kwargs["cwd"] == os.path.abspath("/tmp")
