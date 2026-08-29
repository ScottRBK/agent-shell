"""Public behavioural tests for the opt-in terminal-window execution host."""

import asyncio
import contextlib
import fcntl
import os
import signal
import stat
import sys
import time

import pytest

from agent_shell.execution import (
    IsolationUnavailableError,
    LinuxPidNamespaceIsolation,
    NoIsolation,
    SubprocessTerminalLauncher,
    TerminalWindowExecutionHost,
    TerminalWindowUnavailableError,
)
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


class _FakeTerminalLauncher:
    """External-launcher seam: run the real worker without opening a GUI window."""

    def __init__(self, *, capture_output=False):
        self.command = None
        self.env = None
        self.process = None
        self.capture_output = capture_output
        self.socket_path = None
        self.socket_dir_mode = None
        self.socket_mode = None

    async def launch(self, command, *, cwd, env):
        self.command = list(command)
        self.env = dict(env) if env is not None else None
        self.socket_path = self.command[-1]
        self.socket_dir_mode = stat.S_IMODE(
            os.stat(os.path.dirname(self.socket_path)).st_mode
        )
        self.socket_mode = stat.S_IMODE(os.stat(self.socket_path).st_mode)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=(
                asyncio.subprocess.PIPE
                if self.capture_output
                else asyncio.subprocess.DEVNULL
            ),
            stderr=asyncio.subprocess.PIPE,
        )
        return self.process


class _CompletionTrackedProcess:
    """Process wrapper exposing whether the host closes an already-finished run."""

    def __init__(self, process):
        self._process = process
        self.wait_calls = 0

    @property
    def returncode(self):
        return self._process.returncode

    async def wait(self):
        self.wait_calls += 1
        if self.wait_calls == 1:
            await asyncio.sleep(60)
        return await self._process.wait()

    def terminate(self):
        self._process.terminate()

    def kill(self):
        self._process.kill()


class _CompletionTrackedLauncher(_FakeTerminalLauncher):
    async def launch(self, command, *, cwd, env):
        process = await super().launch(command, cwd=cwd, env=env)
        self.process = _CompletionTrackedProcess(process)
        return self.process


class _DetachingTerminalLauncher(_FakeTerminalLauncher):
    """Launcher whose desktop service exits before its child worker connects back."""

    async def launch(self, command, *, cwd, env):
        async def start_worker_later():
            await asyncio.sleep(0.05)
            self.worker = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            return self.worker

        self.worker = None
        self.worker_task = asyncio.create_task(start_worker_later())
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self.process


class _NeverConnectingTerminalLauncher(_FakeTerminalLauncher):
    async def launch(self, command, *, cwd, env):
        self.command = list(command)
        self.env = dict(env)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self.process


def _lock_is_held(path):
    with open(path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return False


async def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_terminal_host_transports_run_data_without_exposing_it_to_launcher(
    tmp_path,
):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)
    command_secret = "command-secret-only-over-private-ipc"
    secret = "only-over-private-ipc"
    command = [
        sys.executable,
        "-c",
        "import os, sys; "
        "sys.stdout.buffer.write(os.environ['AGENTSHELL_TEST_SECRET'].encode() + "
        "b'\\x00\\xff\\n'); "
        "sys.stderr.buffer.write(b'visible-error\\x00\\xfe\\n')",
        command_secret,
    ]
    run_env = dict(os.environ, AGENTSHELL_TEST_SECRET=secret)

    # Act
    run = await host.launch(command, cwd=str(tmp_path), env=run_env)
    stdout, stderr = await run.communicate()

    # Assert
    assert stdout == secret.encode() + b"\x00\xff\n"
    assert stderr == b"visible-error\x00\xfe\n"
    assert launcher.command is not None
    assert command_secret not in launcher.command
    assert secret not in " ".join(launcher.command)
    assert launcher.env is not None
    assert "AGENTSHELL_TEST_SECRET" not in launcher.env
    assert launcher.socket_dir_mode == 0o700
    assert launcher.socket_mode == 0o600


async def test_terminal_host_is_available_from_the_package_public_api():
    # Arrange
    from agent_shell import TerminalWindowExecutionHost as exported_host

    # Act / Assert
    assert exported_host is TerminalWindowExecutionHost


async def test_terminal_launcher_does_not_receive_unrelated_parent_environment_secrets(
    tmp_path, monkeypatch
):
    # Arrange
    launcher = _FakeTerminalLauncher()
    secret_key = "AGENTSHELL_INHERITED_SECRET"
    secret = "must-stay-with-the-owner"
    monkeypatch.setenv(secret_key, secret)
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act
    run = await host.launch(
        [sys.executable, "-c", f"import os; print(os.environ[{secret_key!r}])"],
        cwd=str(tmp_path),
    )
    stdout, _ = await run.communicate()

    # Assert
    assert stdout == f"{secret}\n".encode()
    assert launcher.env is not None
    assert secret_key not in launcher.env
    assert secret not in launcher.env.values()


async def test_terminal_host_accepts_a_desktop_launcher_that_detaches_before_worker_connects(
    tmp_path,
):
    # Arrange
    launcher = _DetachingTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher, startup_timeout=1.0)

    # Act
    run = await host.launch(
        [sys.executable, "-c", "print('detached worker')"],
        cwd=str(tmp_path),
    )
    stdout, stderr = await run.communicate()
    worker = await launcher.worker_task
    await worker.wait()

    # Assert
    assert stdout == b"detached worker\n"
    assert stderr == b""
    assert run.pid == worker.pid


async def test_terminal_host_times_out_a_launcher_that_never_connects_and_cleans_up(
    tmp_path,
):
    # Arrange
    launcher = _NeverConnectingTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher, startup_timeout=0.05)

    # Act / Assert
    with pytest.raises(TerminalWindowUnavailableError, match="did not connect"):
        await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))

    assert launcher.process is not None
    assert launcher.process.returncode is not None
    assert launcher.command is not None
    assert not os.path.exists(os.path.dirname(launcher.command[-1]))


async def test_terminal_host_cleans_an_abandoned_run_when_owner_exits(tmp_path):
    # Arrange — the owner process deliberately drops the run without releasing it.
    owner_script = r'''
import asyncio
import sys
from agent_shell.execution import TerminalWindowExecutionHost

class Launcher:
    async def launch(self, command, *, cwd, env):
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

async def main():
    run = await TerminalWindowExecutionHost(launcher=Launcher()).launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=sys.argv[1],
    )
    print(run._socket_dir, flush=True)

asyncio.run(main())
'''
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        str(tmp_path),
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    socket_dir = (
        await asyncio.wait_for(owner.stdout.readline(), timeout=5)
    ).decode().strip()

    # Act
    owner_stdout, owner_stderr = await asyncio.wait_for(owner.communicate(), timeout=5)

    # Assert
    assert owner.returncode == 0, owner_stderr.decode() or owner_stdout.decode()
    assert socket_dir
    assert not os.path.exists(socket_dir)


async def test_terminal_worker_cleans_the_socket_when_owner_is_terminated(tmp_path):
    # Arrange
    owner_script = r'''
import asyncio
import sys
from agent_shell.execution import TerminalWindowExecutionHost

class Launcher:
    async def launch(self, command, *, cwd, env):
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

async def main():
    run = await TerminalWindowExecutionHost(launcher=Launcher()).launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=sys.argv[1],
    )
    print(run._socket_dir, flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
'''
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        str(tmp_path),
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    socket_dir = (
        await asyncio.wait_for(owner.stdout.readline(), timeout=5)
    ).decode().strip()

    # Act
    owner.terminate()
    await asyncio.wait_for(owner.communicate(), timeout=5)
    for _ in range(100):
        if not os.path.exists(socket_dir):
            break
        await asyncio.sleep(0.05)

    # Assert
    assert socket_dir
    assert not os.path.exists(socket_dir)


async def test_agentshell_stream_uses_terminal_host_without_adapter_changes(
    tmp_path, monkeypatch
):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'system', 'session_id': 'terminal-session'}), flush=True)\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [\n"
        "    {'type': 'text', 'text': 'from terminal'}\n"
        "]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'is_error': False,\n"
        "    'session_id': 'terminal-session', 'total_cost_usd': 0.01,\n"
        "    'duration_ms': 1, 'usage': {'output_tokens': 2}}), flush=True)\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    launcher = _FakeTerminalLauncher()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=TerminalWindowExecutionHost(launcher=launcher),
    )
    prompt = "prompt-secret-only-over-private-ipc"

    # Act
    events = [
        event
        async for event in shell.stream(cwd=str(tmp_path), prompt=prompt)
    ]

    # Assert
    assert [event.type for event in events] == ["system", "text", "result"]
    assert events[1].content == "from terminal"
    assert events[2].content == "ok"
    assert launcher.command is not None
    assert prompt not in launcher.command


async def test_agentshell_stream_stays_lossless_when_the_visible_terminal_stalls(
    tmp_path, monkeypatch
):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "payload = 'x' * 262144\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [\n"
        "    {'type': 'text', 'text': payload}\n"
        "]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'is_error': False}), flush=True)\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    launcher = _FakeTerminalLauncher(capture_output=True)
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=TerminalWindowExecutionHost(launcher=launcher),
    )

    # Act
    async def collect_events():
        return [
            event
            async for event in shell.stream(cwd=str(tmp_path), prompt="backpressure")
        ]

    stream_task = asyncio.create_task(collect_events())
    try:
        events = await asyncio.wait_for(asyncio.shield(stream_task), timeout=3.0)
    except asyncio.TimeoutError:
        # The cleanup below keeps the intentionally failing red run from orphaning the
        # worker while retaining the timeout as the test failure.
        if launcher.process is not None and launcher.process.returncode is None:
            launcher.process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stream_task, timeout=3.0)
        raise
    finally:
        if launcher.process is not None and launcher.process.returncode is None:
            launcher.process.kill()
        if launcher.process is not None:
            await launcher.process.communicate()

    # Assert
    assert events[0].type == "text"
    assert events[0].content == "x" * 262144
    assert events[1].type == "result"
    assert events[1].content == "ok"


async def test_terminal_host_preserves_target_signal_status(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)
    command = [
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
    ]

    # Act
    run = await host.launch(command, cwd=str(tmp_path))
    returncode = await run.wait()

    # Assert
    assert returncode == -signal.SIGTERM
    assert run.returncode == -signal.SIGTERM


async def test_terminal_host_preserves_target_nonzero_exit_status(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act
    run = await host.launch(
        [sys.executable, "-c", "raise SystemExit(37)"],
        cwd=str(tmp_path),
    )
    returncode = await run.wait()

    # Assert
    assert returncode == 37
    assert run.returncode == 37


async def test_terminal_host_communicate_bridges_pipe_stdin(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)
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

    # Assert
    assert stdout == b"input\x00bytes\n"
    assert stderr == b""


async def test_terminal_host_stdin_rejects_non_bytes_payloads(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    run = await TerminalWindowExecutionHost(launcher=launcher).launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )

    # Act / Assert
    try:
        with pytest.raises(TypeError, match="bytes"):
            run.stdin.write(1)
    finally:
        await run.cancel()


async def test_terminal_host_preserves_success_when_pipe_input_arrives_after_target_exit(
    tmp_path,
):
    # Arrange
    launcher = _FakeTerminalLauncher()
    run = await TerminalWindowExecutionHost(launcher=launcher).launch(
        [sys.executable, "-c", "pass"],
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(0.2)

    # Act
    stdout, stderr = await run.communicate(b"late input")

    # Assert
    assert (stdout, stderr) == (b"", b"")
    assert run.returncode == 0


async def test_terminal_host_ignores_pipe_write_after_target_closes_stdin(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    run = await TerminalWindowExecutionHost(launcher=launcher).launch(
        [
            sys.executable,
            "-c",
            "import os, time; os.close(0); os.close(1); os.close(2); time.sleep(60)",
        ],
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(0.2)

    # Act
    run.stdin.write(b"late input")
    await run.stdin.drain()
    await asyncio.wait_for(run.cancel(), timeout=3.0)

    # Assert
    assert run.returncode == -signal.SIGKILL


async def test_terminal_host_cancellation_uses_ipc_and_reports_sigkill(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)
    run = await host.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )

    # Act
    await run.cancel()

    # Assert
    assert await run.wait() == -signal.SIGKILL
    assert run.returncode == -signal.SIGKILL


async def test_terminal_host_cancel_after_completion_releases_resources(tmp_path):
    # Arrange
    launcher = _CompletionTrackedLauncher()
    run = await TerminalWindowExecutionHost(launcher=launcher).launch(
        [sys.executable, "-c", "pass"],
        cwd=str(tmp_path),
    )
    assert await _wait_until(lambda: run.returncode == 0)
    socket_dir = os.path.dirname(launcher.command[-1])

    # Act
    await run.cancel()

    # Assert
    assert launcher.process.wait_calls >= 2
    assert not os.path.exists(socket_dir)


async def test_terminal_host_release_cancels_an_active_run_and_removes_resources(
    tmp_path, monkeypatch
):
    # Arrange
    lock_path = tmp_path / "released-terminal.lock"
    monkeypatch.setenv("AGENTSHELL_RELEASE_LOCK", str(lock_path))
    launcher = _FakeTerminalLauncher()
    run = await TerminalWindowExecutionHost(launcher=launcher).launch(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, time; lock = open(os.environ['AGENTSHELL_RELEASE_LOCK'], 'w'); "
                "fcntl.flock(lock, fcntl.LOCK_EX); time.sleep(60)"
            ),
        ],
        cwd=str(tmp_path),
    )
    socket_dir = run._socket_dir
    assert await _wait_until(lambda: _lock_is_held(lock_path))

    # Act
    run.release()

    # Assert
    assert await _wait_until(lambda: not _lock_is_held(lock_path))
    assert await _wait_until(lambda: not os.path.exists(socket_dir))


async def test_terminal_host_can_cancel_after_target_closes_its_output_streams(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)
    run = await host.launch(
        [
            sys.executable,
            "-c",
            "import os, time; os.close(1); os.close(2); time.sleep(60)",
        ],
        cwd=str(tmp_path),
    )
    await asyncio.sleep(0.2)

    # Act / Assert
    try:
        await asyncio.wait_for(run.cancel(), timeout=3.0)
    except asyncio.TimeoutError:
        launcher.process.kill()
        await asyncio.wait_for(run.wait(), timeout=3.0)
        pytest.fail("worker stopped listening for cancellation when output reached EOF")

    assert run.returncode == -signal.SIGKILL


async def test_terminal_host_rejects_unsupported_isolation_before_launch(tmp_path):
    # Arrange
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act / Assert
    with pytest.raises(IsolationUnavailableError, match="only NoIsolation"):
        await host.launch(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            isolation_policy=LinuxPidNamespaceIsolation(),
        )
    assert launcher.command is None


async def test_terminal_host_rejects_isolation_policy_subclasses_before_launch(tmp_path):
    # Arrange
    class CustomIsolation(NoIsolation):
        pass

    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act / Assert
    run = None
    try:
        run = await host.launch(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            isolation_policy=CustomIsolation(),
        )
    except IsolationUnavailableError as error:
        assert "only NoIsolation" in str(error)
    else:
        await run.cancel()
        pytest.fail("custom isolation policy was accepted")
    assert launcher.command is None


async def test_terminal_host_rejects_arbitrary_stdin_file_descriptors(tmp_path):
    # Arrange
    read_fd, write_fd = os.pipe()
    launcher = _FakeTerminalLauncher()
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act / Assert
    try:
        with pytest.raises(ValueError, match="DEVNULL or PIPE"):
            await host.launch(
                [sys.executable, "-c", "pass"],
                cwd=str(tmp_path),
                stdin=read_fd,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert launcher.command is None


async def test_terminal_host_supports_concurrent_runs_with_independent_output(tmp_path):
    # Arrange
    host = TerminalWindowExecutionHost(launcher=_FakeTerminalLauncher())
    commands = [
        [
            sys.executable,
            "-c",
            "print('first'); print('first err', file=__import__('sys').stderr)",
        ],
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

    # Assert
    assert results == [
        (b"first\n", b"first err\n"),
        (b"second\n", b"second err\n"),
    ]


async def test_terminal_host_mirrors_each_byte_stream_to_the_visible_worker_terminal(
    tmp_path,
):
    # Arrange
    launcher = _FakeTerminalLauncher(capture_output=True)
    host = TerminalWindowExecutionHost(launcher=launcher)
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'out\\x00\\xff\\n'); "
        "sys.stderr.buffer.write(b'err\\x01\\xfe\\n')",
    ]

    # Act
    run = await host.launch(command, cwd=str(tmp_path))
    stdout, stderr = await run.communicate()
    mirrored_stdout, mirrored_stderr = await launcher.process.communicate()

    # Assert
    assert stdout == mirrored_stdout == b"out\x00\xff\n"
    assert stderr == mirrored_stderr == b"err\x01\xfe\n"


async def test_terminal_host_fails_closed_when_no_launcher_is_available(
    tmp_path, monkeypatch
):
    # Arrange
    monkeypatch.delenv("AGENTSHELL_TERMINAL_LAUNCHER", raising=False)
    monkeypatch.setattr("agent_shell.execution.shutil.which", lambda _name: None)
    host = TerminalWindowExecutionHost()

    # Act / Assert
    with pytest.raises(TerminalWindowUnavailableError, match="no supported terminal launcher"):
        await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))


async def test_terminal_host_reports_missing_graphical_session_before_launch(
    tmp_path, monkeypatch
):
    # Arrange
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    launcher = SubprocessTerminalLauncher("not-a-real-terminal", display="x11")
    host = TerminalWindowExecutionHost(launcher=launcher)

    # Act / Assert
    with pytest.raises(TerminalWindowUnavailableError, match="graphical session"):
        await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))


async def test_abandoned_agentshell_stream_cancels_the_terminal_worker(
    tmp_path, monkeypatch
):
    # Arrange
    lock_path = tmp_path / "terminal-cli.lock"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, json, os, time\n"
        "lock_file = open(os.environ['AGENTSHELL_TERMINAL_LOCK'], 'w')\n"
        "fcntl.flock(lock_file, fcntl.LOCK_EX)\n"
        "print(json.dumps({'type': 'system', 'session_id': 'abandoned'}), flush=True)\n"
        "time.sleep(60)\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTSHELL_TERMINAL_LOCK", str(lock_path))
    launcher = _FakeTerminalLauncher()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=TerminalWindowExecutionHost(launcher=launcher),
    )
    stream = shell.stream(cwd=str(tmp_path), prompt="wait")

    # Act
    event = await anext(stream)
    await asyncio.wait_for(stream.aclose(), timeout=5.0)

    # Assert
    assert event.type == "system"
    assert await _wait_until(lambda: not _lock_is_held(lock_path))


async def test_terminal_window_closure_reports_transport_failure_and_kills_target(
    tmp_path, monkeypatch
):
    # Arrange
    lock_path = tmp_path / "closed-terminal.lock"
    command = [
        sys.executable,
        "-c",
        (
            "import fcntl, os, time; "
            "lock = open(os.environ['AGENTSHELL_CLOSED_LOCK'], 'w'); "
            "fcntl.flock(lock, fcntl.LOCK_EX); time.sleep(60)"
        ),
    ]
    monkeypatch.setenv("AGENTSHELL_CLOSED_LOCK", str(lock_path))
    launcher = _FakeTerminalLauncher()
    run = await TerminalWindowExecutionHost(launcher=launcher).launch(
        command, cwd=str(tmp_path)
    )
    assert await _wait_until(lambda: _lock_is_held(lock_path))

    # Act
    launcher.process.kill()
    returncode = await asyncio.wait_for(run.wait(), timeout=5.0)

    # Assert
    assert returncode == 255
    assert run.returncode == 255
    assert await _wait_until(lambda: not _lock_is_held(lock_path))
