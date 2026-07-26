"""End-to-end process lifecycle: real adapters, real child processes, real signals.

Every other teardown test in the suite patches os.getpgid/os.killpg and hands
release_process a MagicMock whose `pid` is a number no process ever had. That is what let
B1 through: the code inferred "still running" from `process.returncode is None`, which is
false for a child the asyncio child watcher has already reaped, and no mock could show it.

These tests spawn genuine children via a fake CLI on PATH and let the real teardown run
against them, so what is asserted is what the kernel actually did.
"""
import asyncio
import contextlib
import fcntl
import json
import os
import signal
import sys
import time

import pytest

from agent_shell.process_cleanup import _active_process_groups, cleanup_process_groups

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.pi_adapter import PiAdapter

from tests.unit.adapter_matrix import ADAPTERS, OK_RESULT_EVENT

# The executable name each adapter puts at argv[0].
CLI_NAME = {
    ClaudeCodeAdapter: "claude",
    CodexAdapter: "codex",
    OpenCodeAdapter: "opencode",
    CopilotCLIAdapter: "copilot",
    PiAdapter: "pi",
    CursorAdapter: "cursor-agent",
}

# Reads its script from the environment so one file serves every adapter and every scenario.
# The grandchild is spawned first and its pid published before any output, so a consumer that
# has seen one event knows the grandchild exists.
#
# With `grandchild_lock_file` the grandchild holds an exclusive flock for its whole life
# instead of just sleeping. The kernel releases that lock when the last descriptor on it
# closes, which for a SIGKILLed process is at exit, so a test can observe the grandchild's
# death directly rather than inferring it from a pid that may have been recycled.
_FAKE_CLI = '''#!/usr/bin/env python3
import json, os, subprocess, sys, time
spec = json.loads(os.environ["AGENTSHELL_FAKE_CLI"])
if spec.get("grandchild_pid_file"):
    if spec.get("grandchild_lock_file"):
        argv = [sys.executable, "-c",
                "import fcntl, sys, time\\n"
                "f = open(sys.argv[1], 'w')\\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\\n"
                "time.sleep(60)\\n",
                spec["grandchild_lock_file"]]
    else:
        argv = ["sleep", "60"]
    grandchild = subprocess.Popen(argv)
    with open(spec["grandchild_pid_file"], "w") as f:
        f.write(str(grandchild.pid))
for event in spec["stdout"]:
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()
if spec.get("hang"):
    time.sleep(60)
sys.exit(0)
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Install a fake binary for every adapter on PATH; returns a script-setting callable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in CLI_NAME.values():
        target = bin_dir / name
        target.write_text(_FAKE_CLI)
        target.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def set_script(**spec):
        monkeypatch.setenv("AGENTSHELL_FAKE_CLI", json.dumps(spec))

    return set_script


def _reaped(pid: int, timeout: float = 5.0) -> bool:
    """True once `pid` is gone from the process table, or is a zombie awaiting its reaper."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().split(") ", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.01)
    return False


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """True once `predicate` holds; False if it never does within `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _lock_is_held(path: str) -> bool:
    """True while some other process holds the exclusive flock on `path`."""
    with open(path, "a") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False


def _pid_is_gone(pid: int) -> bool:
    """True once no process holds `pid` — the shape a reaped child leaves behind."""
    try:
        os.getpgid(pid)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_completed_stream_kills_nothing_and_leaves_nothing_registered(
        adapter_cls, fake_cli, tmp_path, monkeypatch):
    # Arrange — B1: on the normal path the child is awaited, so it has certainly exited and
    # its pid may already have been recycled. Teardown must not signal anything at all. This
    # is asserted against a real pid rather than a MagicMock's, because the whole defect was
    # a claim about what a real pid means.
    adapter = adapter_cls()
    fake_cli(stdout=[OK_RESULT_EVENT[adapter_cls]])
    _active_process_groups.clear()

    signals = []
    monkeypatch.setattr("agent_shell.process_cleanup.os.killpg",
                        lambda pgid, sig: signals.append((pgid, sig)))

    # Act
    child = None
    events = []
    async for event in adapter.stream(cwd=str(tmp_path), prompt="ping"):
        if child is None:
            child = adapter._active_processes[0]
        events.append(event)

    # Assert
    assert any(e.type == "result" for e in events), "fake CLI did not drive a full run"
    assert signals == [], f"teardown signalled a process group after a clean exit: {signals}"
    assert child.returncode is not None, "child was not reaped on the normal path"
    assert adapter._active_processes == []
    assert child.pid not in _active_process_groups

    # Cleanup
    _active_process_groups.clear()


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_abandoned_stream_really_kills_the_child_and_its_grandchild(
        adapter_cls, fake_cli, tmp_path):
    # Arrange — the other half of the contract, and the guard against over-correcting B1 into
    # never killing anything: a consumer that walks away from a live child must take down the
    # whole session, grandchildren included. That is why teardown uses killpg on a setsid'd
    # child rather than signalling the pid. Real signals here, nothing patched.
    adapter = adapter_cls()
    pid_file = tmp_path / "grandchild.pid"
    fake_cli(stdout=[OK_RESULT_EVENT[adapter_cls]], hang=True,
             grandchild_pid_file=str(pid_file))
    _active_process_groups.clear()

    agen = adapter.stream(cwd=str(tmp_path), prompt="ping")
    await agen.__anext__()
    child = adapter._active_processes[0]
    grandchild_pid = int(pid_file.read_text())
    assert child.returncode is None, "child exited before the abandonment under test"
    assert child.pid in _active_process_groups

    # Act — aclose() is what CPython eventually runs for a consumer that `break`s out of the
    # `async for`; calling it directly removes the scheduling delay described below.
    await agen.aclose()

    # Assert
    await child.wait()
    assert child.returncode != 0, "abandoned child was not killed"
    assert _reaped(grandchild_pid), "grandchild outlived the process group kill"
    assert adapter._active_processes == []
    assert child.pid not in _active_process_groups

    # Cleanup
    _active_process_groups.clear()


async def test_cleanup_kills_the_grandchildren_of_an_already_reaped_child(fake_cli, tmp_path):
    # Arrange — the leak the getpgid(pid) == pid guard used to accept. The CLI exits on its
    # own and the child watcher reaps it, so no process holds its pid any more and
    # os.getpgid() raises. Its grandchild is still running, still in the process group the
    # child created with setsid, and that group is now the only handle on it. This bites
    # hardest on the atexit path — by interpreter exit the child has usually been reaped —
    # so cleanup_process_groups() is what gets called here.
    adapter = ClaudeCodeAdapter()
    pid_file = tmp_path / "grandchild.pid"
    lock_file = tmp_path / "grandchild.lock"
    fake_cli(stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]],
             grandchild_pid_file=str(pid_file), grandchild_lock_file=str(lock_file))
    _active_process_groups.clear()

    agen = adapter.stream(cwd=str(tmp_path), prompt="ping")
    await agen.__anext__()
    child = adapter._active_processes[0]
    grandchild_pid = int(pid_file.read_text())
    try:
        # The lock being held is the positive control: it proves the grandchild is alive and
        # that losing the lock later can only mean it died.
        assert _wait_until(lambda: _lock_is_held(str(lock_file))), \
            "grandchild never took the lock that marks it alive"
        assert _wait_until(lambda: _pid_is_gone(child.pid)), "child was never reaped"
        assert child.pid in _active_process_groups

        # Act
        cleanup_process_groups()

        # Assert
        assert _wait_until(lambda: not _lock_is_held(str(lock_file))), \
            "grandchild outlived the cleanup: it is still holding its lock"
        assert _reaped(grandchild_pid)
        assert child.pid not in _active_process_groups
    finally:
        # Cleanup — the grandchild must never survive this test, however it ended.
        with contextlib.suppress(OSError):
            os.kill(grandchild_pid, signal.SIGKILL)
        await agen.aclose()
        _active_process_groups.clear()


async def test_teardown_after_break_is_deferred_to_a_later_loop_turn(fake_cli, tmp_path):
    # Arrange — N2, documented rather than fixed. CPython does not run an async generator's
    # `finally` synchronously at `break`; it schedules an async_generator_athrow task. The
    # child is therefore still alive and still registered on the statement after the `break`,
    # and if the loop is torn down before that task is scheduled (asyncio.run cancelling
    # pending tasks) the teardown never runs at all — which is what the atexit net covers.
    # Pinning the behaviour here stops the adapters' `finally` comment drifting back into
    # claiming teardown happens at the `break`.
    adapter = ClaudeCodeAdapter()
    pid_file = tmp_path / "grandchild.pid"
    fake_cli(stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]], hang=True,
             grandchild_pid_file=str(pid_file))
    _active_process_groups.clear()

    # Act
    async for event in adapter.stream(cwd=str(tmp_path), prompt="ping"):
        if event.type == "result":
            break
    child = adapter._active_processes[0]
    still_registered_right_after_break = child.pid in _active_process_groups

    # Assert — teardown has NOT happened yet...
    assert still_registered_right_after_break, "teardown ran synchronously at the break"
    assert child.returncode is None

    # ...and lands once the loop gets a turn.
    for _ in range(100):
        if child.pid not in _active_process_groups:
            break
        await asyncio.sleep(0.01)
    assert child.pid not in _active_process_groups, "deferred teardown never ran"

    # Cleanup
    await child.wait()
    _active_process_groups.clear()
