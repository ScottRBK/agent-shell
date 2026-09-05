"""Public behaviour for the optional tmux execution host."""

import asyncio
import fcntl
import os
import signal
import sys
import time

import pytest

from agent_shell import TmuxPlacement
from agent_shell import execution as execution_module
from agent_shell.execution import (
    IsolationUnavailableError,
    LinuxPidNamespaceIsolation,
    TmuxExecutionHost,
    TmuxUnavailableError,
)
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


@pytest.fixture
def fake_tmux(monkeypatch, tmp_path):
    """Replace the external tmux boundary while keeping the real bridge process and IPC."""
    registry = tmp_path / "tmux-registry"
    registry.mkdir()
    bin_dir = tmp_path / "tmux-bin"
    bin_dir.mkdir()
    fake = bin_dir / "tmux"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, signal, subprocess, sys\n"
        "registry = pathlib.Path(os.environ['AGENTSHELL_FAKE_TMUX_REGISTRY'])\n"
        "args = sys.argv[1:]\n"
        "if 'new-session' in args:\n"
        "    session = args[args.index('-s') + 1]\n"
        "    if (registry / session).exists():\n"
        "        raise SystemExit(1)\n"
        "    command = args[args.index('--') + 1:]\n"
        "    argv_file = os.environ.get('AGENTSHELL_FAKE_TMUX_ARGV_FILE')\n"
        "    if argv_file:\n"
        "        pathlib.Path(argv_file).write_text('\\0'.join(args))\n"
        "    worker = subprocess.Popen(command, stdin=subprocess.DEVNULL,\n"
        "                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "                              start_new_session=True)\n"
        "    (registry / session).write_text(str(worker.pid))\n"
        "    raise SystemExit(0)\n"
        "if 'new-window' in args:\n"
        "    session = args[args.index('-t') + 1].removeprefix('=').removesuffix(':')\n"
        "    if not (registry / session).exists():\n"
        "        raise SystemExit(1)\n"
        "    command = args[args.index('--') + 1:]\n"
        "    window = session + '-window-' + str(os.getpid()) + '-' + "
        "str(len(list(registry.iterdir())))\n"
        "    worker = subprocess.Popen(command, stdin=subprocess.DEVNULL,\n"
        "                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "                              start_new_session=True)\n"
        "    (registry / window).write_text(str(worker.pid))\n"
        "    argv_file = os.environ.get('AGENTSHELL_FAKE_TMUX_ARGV_FILE')\n"
        "    if argv_file:\n"
        "        pathlib.Path(argv_file).write_text('\\0'.join(args))\n"
        "    if os.environ.get('AGENTSHELL_FAKE_TMUX_EMPTY_WINDOW_ID'):\n"
        "        print('', flush=True)\n"
        "    else:\n"
        "        print(window, flush=True)\n"
        "    raise SystemExit(0)\n"
        "if 'display-message' in args:\n"
        "    print(os.environ.get('AGENTSHELL_FAKE_TMUX_CURRENT_SESSION', ''))\n"
        "    raise SystemExit(0)\n"
        "if 'kill-session' in args:\n"
        "    session = args[args.index('-t') + 1]\n"
        "    marker = registry / session\n"
        "    try:\n"
        "        os.kill(int(marker.read_text()), signal.SIGKILL)\n"
        "    except (FileNotFoundError, ProcessLookupError, ValueError):\n"
        "        pass\n"
        "    marker.unlink(missing_ok=True)\n"
        "    raise SystemExit(0)\n"
        "if 'kill-window' in args:\n"
        "    window = args[args.index('-t') + 1]\n"
        "    marker = registry / window\n"
        "    try:\n"
        "        os.kill(int(marker.read_text()), signal.SIGKILL)\n"
        "    except (FileNotFoundError, ProcessLookupError, ValueError):\n"
        "        pass\n"
        "    marker.unlink(missing_ok=True)\n"
        "    raise SystemExit(0)\n"
        "if 'list-panes' in args:\n"
        "    for marker in sorted(registry.iterdir()):\n"
        "        print(marker.read_text())\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTSHELL_FAKE_TMUX_REGISTRY", str(registry))
    return registry


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


async def _pane_pids() -> list[str]:
    """Read pane PIDs without making assumptions about the user's tmux sessions."""
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


async def test_tmux_host_preserves_streams_status_and_owned_pane_cleanup(fake_tmux, tmp_path):
    # Arrange
    host = TmuxExecutionHost()
    command = [
        sys.executable,
        "-c",
        (
            "import os, sys; "
            "print(os.getcwd()); "
            "print(os.environ['AGENTSHELL_TMUX_TEST']); "
            "print('stderr bytes', file=sys.stderr)"
        ),
    ]
    run = await host.launch(
        command,
        cwd=str(tmp_path),
        env={"AGENTSHELL_TMUX_TEST": "environment bytes"},
    )

    # Act
    stdout = await run.stdout.read()
    stderr = await run.stderr.read()
    returncode = await run.wait()
    owned_pid = run.pid
    panes_while_retained = await _pane_pids()
    run.release()
    panes_after_release = await _pane_pids()

    # Assert
    assert stdout == f"{tmp_path}\nenvironment bytes\n".encode()
    assert stderr == b"stderr bytes\n"
    assert returncode == 0
    assert run.returncode == 0
    assert str(owned_pid) in panes_while_retained
    assert str(owned_pid) not in panes_after_release


async def test_new_session_placement_uses_explicit_session_and_owns_it(fake_tmux, tmp_path):
    # Arrange
    placement = TmuxPlacement.new_session(name="named-agent-session")
    host = TmuxExecutionHost(placement=placement)

    # Act
    run = await host.launch(
        [sys.executable, "-c", "print('placed')"],
        cwd=str(tmp_path),
    )
    await run.communicate()

    # Assert
    assert (fake_tmux / "named-agent-session").exists()
    run.release()
    assert not (fake_tmux / "named-agent-session").exists()


async def test_new_window_placement_borrows_session_and_owns_only_new_window(
    fake_tmux, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "borrowed-session"
    borrowed_session.write_text(str(os.getpid()))
    placement = TmuxPlacement.new_window(session="borrowed-session")
    host = TmuxExecutionHost(placement=placement)

    # Act
    run = await host.launch(
        [sys.executable, "-c", "print('window placed')"],
        cwd=str(tmp_path),
    )
    await run.communicate()
    created_windows = list(fake_tmux.glob("borrowed-session-window-*"))

    # Assert
    assert len(created_windows) == 1
    run.release()
    assert borrowed_session.exists()
    assert not list(fake_tmux.glob("borrowed-session-window-*"))
    borrowed_session.unlink()


async def test_new_window_placement_exactly_targets_numeric_session(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "6"
    borrowed_session.write_text(str(os.getpid()))
    argv_file = tmp_path / "tmux-argv"
    monkeypatch.setenv("AGENTSHELL_FAKE_TMUX_ARGV_FILE", str(argv_file))
    host = TmuxExecutionHost(placement=TmuxPlacement.new_window(session="6"))

    # Act
    try:
        run = await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
        await run.communicate()
        tmux_args = argv_file.read_text().split("\0")
        run.release()
    finally:
        borrowed_session.unlink(missing_ok=True)

    # Assert
    assert tmux_args[tmux_args.index("-t") + 1] == "=6:"


async def test_current_session_placement_fails_clearly_outside_tmux(
    monkeypatch, tmp_path
):
    # Arrange
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    placement = TmuxPlacement.current_session()
    host = TmuxExecutionHost(placement=placement)

    # Act / Assert
    with pytest.raises(TmuxUnavailableError, match="inside tmux"):
        await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))


async def test_current_session_placement_borrows_current_session(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "current-session"
    owner = await asyncio.create_subprocess_exec("sleep", "60")
    borrowed_session.write_text(str(owner.pid))
    monkeypatch.setenv("TMUX", "/tmp/test-tmux/default,1,1")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("AGENTSHELL_FAKE_TMUX_CURRENT_SESSION", "current-session")
    host = TmuxExecutionHost(placement=TmuxPlacement.current_session())

    # Act
    try:
        run = await host.launch(
            [sys.executable, "-c", "print('current window placed')"],
            cwd=str(tmp_path),
        )
        await run.communicate()
        created_windows = list(fake_tmux.glob("current-session-window-*"))
        run.release()
    finally:
        owner.terminate()
        await owner.wait()

    # Assert
    assert len(created_windows) == 1
    assert borrowed_session.exists()
    assert not list(fake_tmux.glob("current-session-window-*"))
    borrowed_session.unlink()


async def test_unnamed_new_session_placement_generates_a_session_per_launch(
    fake_tmux, tmp_path
):
    # Arrange
    placement = TmuxPlacement.new_session()
    host = TmuxExecutionHost(placement=placement)
    first = await host.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )

    # Act
    try:
        second = await host.launch(
            [sys.executable, "-c", "print('second launch')"],
            cwd=str(tmp_path),
        )
        await second.communicate()
        second.release()
    finally:
        first.release()

    # Assert
    assert len(list(fake_tmux.iterdir())) == 0


async def test_explicit_session_collision_fails_closed_without_killing_existing_session(
    fake_tmux, tmp_path
):
    # Arrange
    existing_session = fake_tmux / "already-owned"
    owner = await asyncio.create_subprocess_exec("sleep", "60")
    existing_session.write_text(str(owner.pid))
    host = TmuxExecutionHost(
        placement=TmuxPlacement.new_session(name="already-owned")
    )

    # Act / Assert
    try:
        with pytest.raises(TmuxUnavailableError, match="could not create"):
            await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
        assert existing_session.exists()
        assert owner.returncode is None
    finally:
        owner.terminate()
        await owner.wait()
        existing_session.unlink()


async def test_unidentifiable_new_window_preserves_borrowed_session(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "window-owner"
    owner = await asyncio.create_subprocess_exec("sleep", "60")
    borrowed_session.write_text(str(owner.pid))
    monkeypatch.setenv("AGENTSHELL_FAKE_TMUX_EMPTY_WINDOW_ID", "1")
    host = TmuxExecutionHost(
        placement=TmuxPlacement.new_window(session="window-owner")
    )

    # Act / Assert
    try:
        with pytest.raises(TmuxUnavailableError, match="window id"):
            await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
        assert borrowed_session.exists()
    finally:
        owner.terminate()
        await owner.wait()
        for window in fake_tmux.glob("window-owner-window-*"):
            try:
                os.kill(int(window.read_text()), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, ValueError):
                pass
            window.unlink(missing_ok=True)
        borrowed_session.unlink()


async def test_new_window_does_not_focus_by_default_and_can_focus_when_requested(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "focus-session"
    owner = await asyncio.create_subprocess_exec("sleep", "60")
    borrowed_session.write_text(str(owner.pid))
    argv_file = tmp_path / "tmux-argv"
    monkeypatch.setenv("AGENTSHELL_FAKE_TMUX_ARGV_FILE", str(argv_file))

    # Act
    try:
        default_run = await TmuxExecutionHost(
            placement=TmuxPlacement.new_window(session="focus-session")
        ).launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
        await default_run.communicate()
        default_args = argv_file.read_text().split("\0")
        default_run.release()

        focused_run = await TmuxExecutionHost(
            placement=TmuxPlacement.new_window(session="focus-session", focus=True)
        ).launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
        await focused_run.communicate()
        focused_args = argv_file.read_text().split("\0")
        focused_run.release()
    finally:
        owner.terminate()
        await owner.wait()
        borrowed_session.unlink(missing_ok=True)

    # Assert
    assert "-d" in default_args
    assert "-d" not in focused_args


async def test_missing_borrowed_session_fails_closed_without_creating_new_session(
    fake_tmux, tmp_path
):
    # Arrange
    host = TmuxExecutionHost(
        placement=TmuxPlacement.new_window(session="does-not-exist")
    )

    # Act / Assert
    with pytest.raises(TmuxUnavailableError, match="could not create"):
        await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    assert list(fake_tmux.iterdir()) == []


async def test_cancelling_borrowed_window_preserves_session_and_removes_window(
    fake_tmux, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "cancel-session"
    owner = await asyncio.create_subprocess_exec("sleep", "60")
    borrowed_session.write_text(str(owner.pid))
    host = TmuxExecutionHost(
        placement=TmuxPlacement.new_window(session="cancel-session")
    )
    run = await host.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )

    # Act
    try:
        await run.cancel()
    finally:
        owner.terminate()
        await owner.wait()

    # Assert
    assert run.returncode == -signal.SIGKILL
    assert borrowed_session.exists()
    assert not list(fake_tmux.glob("cancel-session-window-*"))
    borrowed_session.unlink()


async def test_failed_borrowed_window_preserves_session_and_removes_window(
    fake_tmux, tmp_path
):
    # Arrange
    borrowed_session = fake_tmux / "failure-session"
    owner = await asyncio.create_subprocess_exec("sleep", "60")
    borrowed_session.write_text(str(owner.pid))
    host = TmuxExecutionHost(
        placement=TmuxPlacement.new_window(session="failure-session")
    )
    run = await host.launch(
        [sys.executable, "-c", "raise SystemExit(7)"],
        cwd=str(tmp_path),
    )

    # Act
    try:
        stdout, stderr = await run.communicate()
        run.release()
    finally:
        owner.terminate()
        await owner.wait()

    # Assert
    assert (stdout, stderr) == (b"", b"")
    assert run.returncode == 7
    assert borrowed_session.exists()
    assert not list(fake_tmux.glob("failure-session-window-*"))
    borrowed_session.unlink()


def test_new_window_placement_rejects_invalid_session_names():
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="non-empty"):
        TmuxPlacement.new_window(session="")
    with pytest.raises(ValueError, match="session name"):
        TmuxPlacement.new_window(session="session:window")
    with pytest.raises(ValueError, match="session name"):
        TmuxPlacement.new_window(session="session.window")
    with pytest.raises(TypeError, match="must be a string"):
        TmuxPlacement.new_window(session=None)


async def test_tmux_host_communicate_bridges_pipe_stdin(fake_tmux, tmp_path):
    # Arrange
    host = TmuxExecutionHost()
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    ]

    # Act
    run = await host.launch(
        command,
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await run.communicate(b"input\x00bytes\n")
    run.release()

    # Assert
    assert stdout == b"input\x00bytes\n"
    assert stderr == b""


async def test_tmux_host_cancellation_reports_signal_and_cleans_owned_session(
    fake_tmux, tmp_path
):
    # Arrange
    host = TmuxExecutionHost()
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    run = await host.launch(command, cwd=str(tmp_path))

    # Act
    await run.cancel()

    # Assert
    assert await run.wait() == -signal.SIGKILL
    assert run.returncode == -signal.SIGKILL


async def test_tmux_host_preserves_signal_exit_status(fake_tmux, tmp_path):
    # Arrange
    host = TmuxExecutionHost()
    command = [
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
    ]
    run = await host.launch(command, cwd=str(tmp_path))

    # Act
    await run.stdout.read()
    await run.stderr.read()
    returncode = await run.wait()
    run.release()

    # Assert
    assert returncode == -signal.SIGTERM
    assert run.returncode == -signal.SIGTERM


async def test_tmux_host_rejects_unavailable_tmux_without_native_fallback(monkeypatch, tmp_path):
    # Arrange
    monkeypatch.setattr(execution_module.shutil, "which", lambda _: None)
    host = TmuxExecutionHost()

    # Act / Assert
    with pytest.raises(TmuxUnavailableError, match="requires the optional `tmux`"):
        await host.launch([sys.executable, "-c", "raise SystemExit(99)"], cwd=str(tmp_path))


async def test_tmux_host_rejects_isolation_before_creating_resources(tmp_path):
    # Arrange
    host = TmuxExecutionHost()
    policy = LinuxPidNamespaceIsolation()

    # Act / Assert
    with pytest.raises(IsolationUnavailableError, match="only NoIsolation"):
        await host.launch(
            [sys.executable, "-c", "raise SystemExit(99)"],
            cwd=str(tmp_path),
            isolation_policy=policy,
        )


async def test_tmux_host_rejects_arbitrary_stdin_file_descriptors(tmp_path):
    # Arrange
    read_fd, write_fd = os.pipe()
    host = TmuxExecutionHost()

    # Act / Assert
    try:
        with pytest.raises(TmuxUnavailableError, match="DEVNULL or PIPE"):
            await host.launch(
                [sys.executable, "-c", "pass"],
                cwd=str(tmp_path),
                stdin=read_fd,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


async def test_tmux_host_supports_concurrent_runs_with_independent_output(fake_tmux, tmp_path):
    # Arrange
    host = TmuxExecutionHost()
    commands = [
        [sys.executable, "-c", "print('first'); print('first err', file=__import__('sys').stderr)"],
        [
            sys.executable,
            "-c",
            "print('second'); print('second err', file=__import__('sys').stderr)",
        ],
    ]

    # Act
    runs = await asyncio.gather(
        *(host.launch(command, cwd=str(tmp_path)) for command in commands)
    )
    results = await asyncio.gather(*(run.communicate() for run in runs))
    for run in runs:
        run.release()

    # Assert
    assert results == [
        (b"first\n", b"first err\n"),
        (b"second\n", b"second err\n"),
    ]


async def test_tmux_bootstrap_does_not_put_target_command_or_environment_in_argv(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    argv_file = tmp_path / "tmux-argv"
    monkeypatch.setenv("AGENTSHELL_FAKE_TMUX_ARGV_FILE", str(argv_file))
    host = TmuxExecutionHost()
    command_secret = "command-secret-not-in-tmux-argv"
    environment_secret = "environment-secret-not-in-tmux-argv"

    # Act
    run = await host.launch(
        [sys.executable, "-c", "import os; print(os.environ['SECRET'])", command_secret],
        cwd=str(tmp_path),
        env={"SECRET": environment_secret},
    )
    stdout, stderr = await run.communicate()
    run.release()

    # Assert
    argv = argv_file.read_text()
    assert command_secret not in argv
    assert environment_secret not in argv
    assert stdout == f"{environment_secret}\n".encode()
    assert stderr == b""


async def test_tmux_cleanup_keeps_an_unrelated_session_marker(fake_tmux, tmp_path):
    # Arrange
    unrelated = fake_tmux / "unrelated-session"
    unrelated.write_text(str(os.getpid()))
    host = TmuxExecutionHost()

    # Act
    run = await host.launch([sys.executable, "-c", "print('owned')"], cwd=str(tmp_path))
    await run.communicate()
    run.release()

    # Assert
    assert unrelated.exists()
    unrelated.unlink()


async def test_agentshell_execution_surfaces_use_tmux_host_without_adapter_changes(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'system', 'session_id': 'tmux-session'}), flush=True)\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [\n"
        "    {'type': 'text', 'text': 'from tmux'}\n"
        "]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'is_error': False,\n"
        "    'session_id': 'tmux-session', 'total_cost_usd': 0.01,\n"
        "    'duration_ms': 1, 'usage': {'output_tokens': 2}}), flush=True)\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=TmuxExecutionHost(),
    )

    # Act
    events = [
        event
        async for event in shell.stream(cwd=str(tmp_path), prompt="say hello")
    ]

    # Assert
    assert [event.type for event in events] == ["system", "text", "result"]
    assert events[1].content == "from tmux"
    assert events[2].content == "ok"

    response = await shell.execute(cwd=str(tmp_path), prompt="say hello")
    health = await shell.health_check(cwd=str(tmp_path), model="test-model")
    assert response.response == "from tmux"
    assert response.output_tokens == 2
    assert health.healthy is True


async def test_abandoned_agentshell_stream_cancels_the_tmux_owned_process(
    fake_tmux, monkeypatch, tmp_path
):
    # Arrange
    lock_path = tmp_path / "cli.lock"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, json, os, time\n"
        "lock_file = open(os.environ['AGENTSHELL_TMUX_LOCK_FILE'], 'w')\n"
        "fcntl.flock(lock_file, fcntl.LOCK_EX)\n"
        "print(json.dumps({'type': 'system', 'session_id': 'abandoned'}), flush=True)\n"
        "time.sleep(60)\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTSHELL_TMUX_LOCK_FILE", str(lock_path))
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=TmuxExecutionHost(),
    )
    stream = shell.stream(cwd=str(tmp_path), prompt="wait")

    # Act
    event = await anext(stream)
    await asyncio.wait_for(stream.aclose(), timeout=5.0)

    # Assert
    assert event.type == "system"
    assert await _wait_until(lambda: not _lock_is_held(lock_path))
