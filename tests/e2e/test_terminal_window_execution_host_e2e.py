"""Local-only smoke coverage for the real graphical terminal host."""

import asyncio
import contextlib
import fcntl
import os
import signal
import sys
import time

import pytest

from agent_shell import (
    TerminalWindowExecutionHost,
    TerminalWindowUnavailableError,
    discover_terminal_launcher,
)

pytestmark = pytest.mark.e2e


def _lock_is_held(path) -> bool:
    with open(path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return False


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _real_launcher_or_skip():
    try:
        launcher = discover_terminal_launcher()
    except TerminalWindowUnavailableError as error:
        pytest.skip(str(error))
    return _ObservedLauncher(launcher)


class _ObservedLauncher:
    """Delegate to the real launcher while exposing its public process outcome to the test."""

    def __init__(self, launcher):
        self._launcher = launcher
        self.display = launcher.display
        self.requires_graphical = launcher.requires_graphical
        self.process = None

    async def launch(self, command, *, cwd, env):
        self.process = await self._launcher.launch(command, cwd=cwd, env=env)
        return self.process


def _launcher_attached(launcher) -> bool:
    """Return whether the launcher process remained attached after host launch.

    The launcher protocol exposes a process handle, not a portable GUI-window identity.  A
    detached terminal may therefore outlive its launcher process without being observable here.
    """
    process = launcher.process
    return process is not None and process.returncode is None


def _launcher_finished(launcher) -> bool:
    process = launcher.process
    return process is not None and process.returncode is not None


async def _launch_real_or_skip(host, command, *, cwd):
    try:
        return await host.launch(command, cwd=cwd)
    except TerminalWindowUnavailableError as error:
        detail = str(error)
        if any(
            marker in detail
            for marker in (
                "Operation not permitted",
                "no usable graphical session",
                "could not start terminal launcher",
                "exited before starting worker (status -6)",
            )
        ):
            pytest.skip(f"real graphical terminal is unavailable: {error}")
        raise


async def test_real_terminal_window_host_runs_and_cleans_owned_target(tmp_path):
    # Arrange
    launcher = _real_launcher_or_skip()
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act
    run = await _launch_real_or_skip(
        host,
        [
            sys.executable,
            "-c",
            (
                "import sys; print('real terminal stdout'); "
                "print('real terminal stderr', file=sys.stderr)"
            ),
        ],
        cwd=str(tmp_path),
    )
    try:
        target_pid = run.pid
        launcher_was_attached = _launcher_attached(launcher)
        stdout, stderr = await run.communicate()
        target_gone = await _wait_until(lambda: not _pid_exists(target_pid))
        launcher_finished = (
            await _wait_until(lambda: _launcher_finished(launcher))
            if launcher_was_attached
            else None
        )
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run.cancel(), timeout=5.0)
        run.release()

    # Assert
    assert stdout == b"real terminal stdout\n"
    assert stderr == b"real terminal stderr\n"
    assert run.returncode == 0
    assert target_gone
    if launcher_was_attached:
        assert launcher_finished


async def test_real_terminal_window_host_cancels_target_and_cleans_owned_resources(
    tmp_path,
):
    # Arrange
    launcher = _real_launcher_or_skip()
    lock_path = tmp_path / "real-terminal-target.lock"
    host = TerminalWindowExecutionHost(launcher=launcher)
    run = await _launch_real_or_skip(
        host,
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time; "
                "lock = open(sys.argv[1], 'w'); "
                "fcntl.flock(lock, fcntl.LOCK_EX); time.sleep(60)"
            ),
            str(lock_path),
        ],
        cwd=str(tmp_path),
    )
    target_pid = run.pid
    launcher_was_attached = _launcher_attached(launcher)

    # Act
    try:
        target_started = await _wait_until(lambda: _lock_is_held(lock_path))
        await asyncio.wait_for(run.cancel(), timeout=10.0)
        target_stopped = await _wait_until(lambda: not _lock_is_held(lock_path))
        target_gone = await _wait_until(lambda: not _pid_exists(target_pid))
        launcher_finished = (
            await _wait_until(lambda: _launcher_finished(launcher))
            if launcher_was_attached
            else None
        )
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run.cancel(), timeout=5.0)
        run.release()

    # Assert
    assert target_started
    assert run.returncode == -signal.SIGKILL
    assert target_stopped
    assert target_gone
    if launcher_was_attached:
        assert launcher_finished
