"""Local-only smoke coverage for the real tmux executable and server."""

import asyncio
import fcntl
import shutil
import signal
import sys
import time

import pytest

from agent_shell import TmuxExecutionHost, TmuxPlacement, TmuxUnavailableError

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def isolated_tmux_server(monkeypatch, tmp_path):
    """Keep real-tmux smoke tests on a temporary socket/server, never the user's server."""
    socket_directory = tmp_path / "tmux-sockets"
    socket_directory.mkdir()
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_directory))


def _lock_is_held(path) -> bool:
    with open(path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return False


async def _pane_pids() -> list[str]:
    """Return pane PIDs from tmux's public listing command."""
    process = await asyncio.create_subprocess_exec(
        "tmux",
        "-f",
        "/dev/null",
        "list-panes",
        "-a",
        "-F",
        "#{pane_pid}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return []
    return stdout.decode().splitlines()


async def _window_ids(session: str) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        "tmux",
        "-f",
        "/dev/null",
        "list-windows",
        "-t",
        session,
        "-F",
        "#{window_id}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return []
    return stdout.decode().splitlines()


async def _wait_for_pane_pid(pid: int, *, present: bool, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (str(pid) in await _pane_pids()) is present:
            return True
        await asyncio.sleep(0.01)
    return (str(pid) in await _pane_pids()) is present


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _launch_real_or_skip(host, command, *, cwd):
    try:
        return await host.launch(command, cwd=cwd)
    except TmuxUnavailableError as error:
        if "Operation not permitted" in str(error):
            pytest.skip(f"real tmux is unavailable in this environment: {error}")
        raise


async def test_real_tmux_host_runs_and_cleans_a_local_process(tmp_path):
    # Arrange
    if shutil.which("tmux") is None:
        pytest.skip("the optional tmux executable is unavailable")
    host = TmuxExecutionHost()

    # Act
    run = await _launch_real_or_skip(
        host,
        [
            sys.executable,
            "-c",
            "import sys; print('real tmux stdout'); print('real tmux stderr', file=sys.stderr)",
        ],
        cwd=str(tmp_path),
    )
    try:
        stdout, stderr = await run.communicate()
        owned_pid = run.pid
        pane_while_retained = await _wait_for_pane_pid(owned_pid, present=True)
        run.release()
        pane_after_release = await _wait_for_pane_pid(owned_pid, present=False)
    finally:
        run.release()

    # Assert
    assert stdout == b"real tmux stdout\n"
    assert stderr == b"real tmux stderr\n"
    assert run.returncode == 0
    assert pane_while_retained
    assert pane_after_release


async def test_real_tmux_host_cancels_target_and_removes_owned_pane(tmp_path):
    # Arrange
    if shutil.which("tmux") is None:
        pytest.skip("the optional tmux executable is unavailable")
    lock_path = tmp_path / "real-tmux-target.lock"
    host = TmuxExecutionHost()
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
    owned_pid = run.pid

    # Act
    try:
        target_started = await _wait_until(lambda: _lock_is_held(lock_path))
        pane_while_running = await _wait_for_pane_pid(owned_pid, present=True)
        await asyncio.wait_for(run.cancel(), timeout=5.0)
        target_stopped = await _wait_until(lambda: not _lock_is_held(lock_path))
        pane_after_cancel = await _wait_for_pane_pid(owned_pid, present=False)
    finally:
        run.release()

    # Assert
    assert target_started
    assert pane_while_running
    assert run.returncode == -signal.SIGKILL
    assert target_stopped
    assert pane_after_cancel


async def test_real_tmux_new_window_cleanup_preserves_borrowed_session(tmp_path):
    # Arrange
    if shutil.which("tmux") is None:
        pytest.skip("the optional tmux executable is unavailable")
    session = "agentshell-borrowed-e2e"
    setup = await asyncio.create_subprocess_exec(
        "tmux",
        "-f",
        "/dev/null",
        "new-session",
        "-d",
        "-s",
        session,
        "-P",
        "-F",
        "#{window_id}",
        "--",
        "sleep",
        "60",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, setup_stderr = await setup.communicate()
    if setup.returncode != 0:
        if b"Operation not permitted" in setup_stderr:
            pytest.skip("real tmux is unavailable in this environment")
        raise AssertionError(setup_stderr.decode(errors="replace"))
    original_windows = await _window_ids(session)
    host = TmuxExecutionHost(
        placement=TmuxPlacement.new_window(session=session)
    )

    # Act
    run = await _launch_real_or_skip(
        host,
        [sys.executable, "-c", "print('borrowed real window')"],
        cwd=str(tmp_path),
    )
    try:
        await run.communicate()
        windows_while_retained = await _window_ids(session)
        run.release()
        deadline = time.monotonic() + 5.0
        windows_after_release = await _window_ids(session)
        while time.monotonic() < deadline and windows_after_release != original_windows:
            await asyncio.sleep(0.01)
            windows_after_release = await _window_ids(session)
    finally:
        run.release()
        cleanup = await asyncio.create_subprocess_exec(
            "tmux",
            "-f",
            "/dev/null",
            "kill-session",
            "-t",
            session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()

    # Assert
    assert len(windows_while_retained) == len(original_windows) + 1
    assert windows_after_release == original_windows
