"""Herdr execution-host behaviour with real local processes."""

import asyncio
import contextlib
import fcntl
import os
import signal
import sys

import pytest

from agent_shell.execution import (
    HerdrExecutionHost,
    IsolationUnavailableError,
)
from agent_shell.herdr import HerdrPane, HerdrUnavailableError
from agent_shell.models.agent import AgentExecutionError, AgentType
from agent_shell.shell import AgentShell
from tests.unit import (
    codex_fixtures,
    copilot_fixtures,
    cursor_fixtures,
    grok_fixtures,
    opencode_fixtures,
    pi_fixtures,
)
from tests.unit import fixtures as claude_fixtures


def _lock_is_held(path: str) -> bool:
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


class _FakeHerdrPane:
    def __init__(self, pane_id, process):
        self.pane_id = pane_id
        self.process = process


class _FakeHerdrClient:
    """External Herdr boundary; the worker itself is still a real subprocess."""

    def __init__(self, pane_output=None):
        self.requests = []
        self.closed_panes = []
        self.closed = asyncio.Event()
        self._next_pane = 1
        self._pane_output = pane_output
        self._pane_handles = {}

    async def create_pane(self, *, cwd, argv, label):
        pane_stdout = pane_stderr = None
        if self._pane_output is not None:
            pane_stdout = os.open(
                self._pane_output,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            pane_stderr = os.open(
                self._pane_output,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=(
                pane_stdout
                if pane_stdout is not None
                else asyncio.subprocess.DEVNULL
            ),
            stderr=(
                pane_stderr
                if pane_stderr is not None
                else asyncio.subprocess.DEVNULL
            ),
        )
        pane_id = f"fake:pane-{self._next_pane}"
        self._next_pane += 1
        if pane_stdout is not None:
            self._pane_handles[pane_id] = (pane_stdout, pane_stderr)
        self.requests.append({"cwd": cwd, "argv": list(argv), "label": label})
        return _FakeHerdrPane(pane_id, process)

    async def close_pane(self, pane):
        self.closed_panes.append(pane.pane_id)
        if pane.process.returncode is None:
            pane.process.kill()
        await pane.process.wait()
        for descriptor in self._pane_handles.pop(pane.pane_id, ()):
            os.close(descriptor)
        self.closed.set()




async def test_herdr_host_launches_a_command_with_real_streams_and_status(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib, sys; "
            "print(pathlib.Path.cwd()); "
            "print(os.environ['AGENTSHELL_TEST_VALUE']); "
            "print('herdr stderr', file=sys.stderr)"
        ),
    ]

    # Act
    run = await host.launch(
        command,
        cwd=str(tmp_path),
        env={"AGENTSHELL_TEST_VALUE": "from-ipc"},
    )
    stdout = await run.stdout.read()
    stderr = await run.stderr.read()
    returncode = await run.wait()
    run.release()

    # Assert
    assert run.pid > 0
    assert stdout == f"{tmp_path}\nfrom-ipc\n".encode()
    assert stderr == b"herdr stderr\n"
    assert returncode == 0
    assert run.returncode == 0
    worker_argv = client.requests[0]["argv"]
    assert "from-ipc" not in worker_argv
    assert "AGENTSHELL_TEST_VALUE" not in worker_argv
    socket_path = worker_argv[-1]
    assert os.stat(os.path.dirname(socket_path)).st_mode & 0o777 == 0o700
    run.release()
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)
    assert client.closed_panes == ["fake:pane-1"]
    assert not os.path.exists(socket_path)


async def test_herdr_mirrors_target_output_to_the_owned_pane_best_effort(tmp_path):
    # Arrange
    pane_output = tmp_path / "pane-output"
    client = _FakeHerdrClient(pane_output=pane_output)
    host = HerdrExecutionHost(client=client)
    run = await host.launch(
        [
            sys.executable,
            "-c",
            "import sys; print('pane stdout'); print('pane stderr', file=sys.stderr)",
        ],
        cwd=str(tmp_path),
    )

    # Act
    stdout = await run.stdout.read()
    stderr = await run.stderr.read()
    await run.wait()
    run.release()
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)

    # Assert
    assert stdout == b"pane stdout\n"
    assert stderr == b"pane stderr\n"
    mirrored = pane_output.read_bytes()
    assert b"pane stdout\n" in mirrored
    assert b"pane stderr\n" in mirrored


async def test_injected_client_does_not_trigger_unrelated_herdr_cleanup(tmp_path, monkeypatch):
    # Arrange — an injected client owns the pane lifecycle. The worker must not guess that the
    # default Herdr CLI can safely close an arbitrary injected pane id.
    marker = tmp_path / "unexpected-herdr-close"
    herdr_bin = tmp_path / "herdr"
    herdr_bin.write_text(
        "#!/usr/bin/env python3\n"
        f"open({str(marker)!r}, 'a').write('called\\n')\n"
    )
    herdr_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)

    # Act
    run = await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    await run.wait()
    run.release()
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)

    # Assert
    assert not marker.exists()


async def test_herdr_runs_use_distinct_owned_resources(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)

    # Act
    first, second = await asyncio.gather(
        host.launch([sys.executable, "-c", "import time; time.sleep(0.05)"], cwd=str(tmp_path)),
        host.launch([sys.executable, "-c", "import time; time.sleep(0.05)"], cwd=str(tmp_path)),
    )
    await asyncio.gather(first.wait(), second.wait())
    first.release()
    second.release()
    await asyncio.sleep(0.05)

    # Assert
    labels = [request["label"] for request in client.requests]
    sockets = [request["argv"][-1] for request in client.requests]
    assert len(set(labels)) == 2
    assert len(set(sockets)) == 2
    assert client.closed_panes == ["fake:pane-1", "fake:pane-2"]


async def test_herdr_host_cancels_the_owned_run_through_the_bridge(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    run = await host.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )

    # Act
    await asyncio.wait_for(run.cancel(), timeout=1.0)

    # Assert
    assert run.returncode == -signal.SIGKILL
    assert await run.wait() == -signal.SIGKILL
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_herdr_host_cleanup_is_idempotent_after_cancellation(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    run = await host.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )

    # Act
    await run.cancel()
    run.release()
    await asyncio.sleep(0.05)

    # Assert
    assert client.closed_panes == ["fake:pane-1"]


async def test_herdr_host_communicate_returns_subprocess_compatible_tuple(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "data = sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(data.upper()); "
            "sys.stderr.buffer.write(b'bridge stderr\\n')"
        ),
    ]

    # Act
    run = await host.launch(
        command,
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )
    result = await run.communicate(b"hello")
    run.release()

    # Assert
    assert isinstance(result, tuple)
    assert result == (b"HELLO", b"bridge stderr\n")
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_herdr_host_communicate_closes_pipe_stdin_when_no_input_is_given(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    run = await host.launch(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read())"],
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )

    # Act
    stdout, stderr = await asyncio.wait_for(run.communicate(), timeout=1.0)
    run.release()

    # Assert
    assert (stdout, stderr) == (b"b''\n", b"")
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_herdr_host_reports_target_signal_status(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    run = await host.launch(
        [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        cwd=str(tmp_path),
    )

    # Act
    await run.stdout.read()
    await run.stderr.read()
    returncode = await run.wait()
    run.release()

    # Assert
    assert returncode == -signal.SIGTERM
    assert run.returncode == -signal.SIGTERM
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_herdr_cancellation_kills_the_target_process_tree(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    lock_file = tmp_path / "herdr-grandchild.lock"
    grandchild_script = (
        "import fcntl, sys, time\n"
        "handle = open(sys.argv[1], 'w')\n"
        "fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "time.sleep(60)\n"
    )
    target_script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_script!r}, sys.argv[1]], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(60)\n"
    )
    run = await host.launch(
        [sys.executable, "-c", target_script, str(lock_file)],
        cwd=str(tmp_path),
    )
    for _ in range(100):
        if _lock_is_held(str(lock_file)):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("target grandchild never acquired its liveness lock")

    # Act
    await run.cancel()

    # Assert
    assert await run.wait() == -signal.SIGKILL
    for _ in range(100):
        if not _lock_is_held(str(lock_file)):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("target grandchild outlived Herdr cancellation")
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_herdr_host_rejects_arbitrary_stdin_fd_before_creating_a_pane(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)

    # Act / Assert
    with pytest.raises(ValueError, match="only DEVNULL or PIPE"):
        await host.launch(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            stdin=123,
        )

    assert client.requests == []


async def test_herdr_host_rejects_unsupported_isolation_before_creating_a_pane(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)

    # Act / Assert
    with pytest.raises(IsolationUnavailableError, match="only NoIsolation"):
        await host.launch(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            isolation_policy=object(),
        )

    assert client.requests == []


async def test_herdr_host_reports_missing_external_command_without_fallback(tmp_path):
    # Arrange
    host = HerdrExecutionHost(herdr_command="/definitely/missing/herdr")

    # Act / Assert
    with pytest.raises(HerdrUnavailableError, match="is not installed"):
        await host.launch(
            [sys.executable, "-c", "raise SystemExit(88)"],
            cwd=str(tmp_path),
        )


async def test_herdr_host_bounds_cli_cleanup_after_pane_startup_timeout(tmp_path):
    # Arrange
    herdr_bin = tmp_path / "herdr"
    herdr_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "if sys.argv[1:3] == ['workspace', 'create']:\n"
        "    print(json.dumps({'result': {'workspace': {'workspace_id': 'fake:workspace'}, "
        "'root_pane': {'pane_id': 'fake:pane'}}}), flush=True)\n"
        "else:\n"
        "    time.sleep(60)\n"
    )
    herdr_bin.chmod(0o755)
    host = HerdrExecutionHost(
        herdr_command=str(herdr_bin),
        startup_timeout=0.02,
        cleanup_timeout=0.02,
    )

    # Act / Assert
    with pytest.raises(HerdrUnavailableError, match="timed out while creating the Herdr pane"):
        await asyncio.wait_for(
            host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path)),
            timeout=0.5,
        )


async def test_herdr_host_bounds_external_setup_with_startup_timeout(tmp_path):
    # Arrange
    class _HangingHerdrClient:
        def __init__(self):
            self.cancelled = asyncio.Event()

        async def create_pane(self, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        async def close_pane(self, pane):
            pass

    client = _HangingHerdrClient()
    host = HerdrExecutionHost(client=client, startup_timeout=0.02)

    # Act / Assert
    with pytest.raises(HerdrUnavailableError, match="timed out while creating the Herdr pane"):
        await asyncio.wait_for(
            host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path)),
            timeout=0.5,
        )
    await asyncio.wait_for(client.cancelled.wait(), timeout=0.1)


async def test_herdr_host_normalizes_bridge_connection_timeout(tmp_path):
    # Arrange
    class _NoConnectHerdrClient:
        def __init__(self):
            self.closed = asyncio.Event()

        async def create_pane(self, **kwargs):
            del kwargs
            return HerdrPane("fake:pane")

        async def close_pane(self, pane):
            del pane
            self.closed.set()

    client = _NoConnectHerdrClient()
    host = HerdrExecutionHost(client=client, startup_timeout=0.02)

    # Act / Assert
    with pytest.raises(
        HerdrUnavailableError, match="timed out waiting for the Herdr bridge connection"
    ):
        await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    await asyncio.wait_for(client.closed.wait(), timeout=0.1)


async def test_herdr_host_bounds_hung_pane_cleanup(tmp_path):
    # Arrange
    class _HangingCloseHerdrClient(_FakeHerdrClient):
        def __init__(self):
            super().__init__()
            self.close_started = asyncio.Event()

        async def close_pane(self, pane):
            del pane
            self.close_started.set()
            await asyncio.Event().wait()

    client = _HangingCloseHerdrClient()
    host = HerdrExecutionHost(client=client, cleanup_timeout=0.02)
    run = await host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    await run.wait()

    # Act / Assert
    run.release()
    with pytest.raises(
        HerdrUnavailableError, match="timed out while closing Herdr resources"
    ):
        await asyncio.wait_for(run.wait_release(), timeout=0.5)
    await asyncio.wait_for(client.close_started.wait(), timeout=0.1)


async def test_herdr_host_bounds_pane_cleanup_after_startup_failure(tmp_path):
    # Arrange
    class _NoConnectHerdrClient:
        def __init__(self):
            self.close_started = asyncio.Event()
            self.worker_argv = None

        async def create_pane(self, **kwargs):
            self.worker_argv = list(kwargs["argv"])
            return HerdrPane("fake:pane")

        async def close_pane(self, pane):
            del pane
            self.close_started.set()
            await asyncio.Event().wait()

    client = _NoConnectHerdrClient()
    host = HerdrExecutionHost(
        client=client,
        startup_timeout=0.02,
        cleanup_timeout=0.02,
    )

    # Act / Assert
    with pytest.raises(
        HerdrUnavailableError, match="timed out while closing Herdr resources"
    ):
        await asyncio.wait_for(
            host.launch([sys.executable, "-c", "pass"], cwd=str(tmp_path)),
            timeout=0.5,
    )
    await asyncio.wait_for(client.close_started.wait(), timeout=0.1)
    assert client.worker_argv is not None
    assert not os.path.exists(os.path.dirname(client.worker_argv[-1]))


async def test_herdr_host_reports_target_start_failure_during_launch(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)

    # Act / Assert
    with pytest.raises(FileNotFoundError):
        await host.launch(
            ["/definitely/missing/agent-shell-target"],
            cwd=str(tmp_path),
        )

    assert client.closed_panes == ["fake:pane-1"]


async def test_herdr_worker_closes_its_pane_after_host_interpreter_exit(tmp_path):
    # Arrange — this fake CLI models just enough of the external Herdr boundary to start the
    # real worker and record its self-cleanup after the owner disappears.
    herdr_bin = tmp_path / "herdr"
    close_marker = tmp_path / "pane-closed"
    workspace_close_marker = tmp_path / "workspace-closed"
    socket_marker = tmp_path / "socket-path"
    herdr_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, shlex, subprocess, sys\n"
        "if sys.argv[1:3] == ['workspace', 'create']:\n"
        "    print(json.dumps({'result': {'workspace': {'workspace_id': 'fake:workspace'}, "
        "'root_pane': {'pane_id': 'fake:pane'}}}), flush=True)\n"
        "elif sys.argv[1:3] == ['pane', 'run']:\n"
        f"    open({str(socket_marker)!r}, 'w').write(shlex.split(sys.argv[-1])[-1])\n"
        "    subprocess.Popen(sys.argv[-1], shell=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "elif sys.argv[1:3] == ['pane', 'close']:\n"
        f"    open({str(close_marker)!r}, 'a').write(sys.argv[3] + '\\n')\n"
        "elif sys.argv[1:3] == ['workspace', 'close']:\n"
        f"    open({str(workspace_close_marker)!r}, 'a').write(sys.argv[3] + '\\n')\n"
    )
    herdr_bin.chmod(0o755)
    ready = tmp_path / "worker.pid"
    lock_file = tmp_path / "target.lock"
    target_script = (
        "import fcntl, sys, time\n"
        "handle = open(sys.argv[1], 'w')\n"
        "fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "time.sleep(60)\n"
    )
    owner_script = (
        "import asyncio, os, sys\n"
        "from agent_shell.herdr import HerdrExecutionHost\n"
        f"target_script = {target_script!r}\n"
        "async def main():\n"
        "    host = HerdrExecutionHost(herdr_command=sys.argv[1])\n"
        "    run = await host.launch([sys.executable, '-c', target_script, sys.argv[4]], "
        "cwd=sys.argv[2])\n"
        "    open(sys.argv[3], 'w').write(str(run.pid))\n"
        "    os._exit(0)\n"
        "asyncio.run(main())\n"
    )
    owner_env = dict(os.environ)
    owner_env["PATH"] = f"{tmp_path}{os.pathsep}{owner_env['PATH']}"
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        str(herdr_bin),
        str(tmp_path),
        str(ready),
        str(lock_file),
        cwd=str(tmp_path),
        env=owner_env,
    )

    try:
        # Act
        await asyncio.wait_for(owner.wait(), timeout=5.0)
        for _ in range(200):
            bridge_removed = (
                socket_marker.exists()
                and not os.path.exists(os.path.dirname(socket_marker.read_text()))
            )
            if close_marker.exists() and workspace_close_marker.exists() and bridge_removed:
                break
            await asyncio.sleep(0.01)

        # Assert
        assert owner.returncode == 0
        assert close_marker.read_text() == "fake:pane\n"
        assert workspace_close_marker.read_text() == "fake:workspace\n"
        assert not _lock_is_held(str(lock_file))
        assert not os.path.exists(os.path.dirname(socket_marker.read_text()))
    finally:
        if ready.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(ready.read_text()), signal.SIGKILL)


async def test_herdr_worker_bounds_hung_cleanup_cli_calls(tmp_path):
    # Arrange
    herdr_bin = tmp_path / "herdr"
    pane_close_started = tmp_path / "pane-close-started"
    workspace_closed = tmp_path / "workspace-closed"
    worker_pid_file = tmp_path / "worker.pid"
    herdr_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, shlex, subprocess, sys, time\n"
        "if sys.argv[1:3] == ['workspace', 'create']:\n"
        "    print(json.dumps({'result': {'workspace': {'workspace_id': 'fake:workspace'}, "
        "'root_pane': {'pane_id': 'fake:pane'}}}), flush=True)\n"
        "elif sys.argv[1:3] == ['pane', 'run']:\n"
        "    subprocess.Popen(sys.argv[-1], shell=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "elif sys.argv[1:3] == ['pane', 'close']:\n"
        f"    open({str(pane_close_started)!r}, 'a').write('started\\n')\n"
        "    time.sleep(60)\n"
        "elif sys.argv[1:3] == ['workspace', 'close']:\n"
        f"    open({str(workspace_closed)!r}, 'a').write(sys.argv[3] + '\\n')\n"
    )
    herdr_bin.chmod(0o755)
    owner_script = (
        "import asyncio, os, sys\n"
        "from agent_shell.herdr import HerdrExecutionHost\n"
        "async def main():\n"
        "    host = HerdrExecutionHost(herdr_command=sys.argv[1], cleanup_timeout=0.02)\n"
        "    run = await host.launch([sys.executable, '-c', 'pass'], cwd=sys.argv[2])\n"
        "    open(sys.argv[3], 'w').write(str(run.pid))\n"
        "    os._exit(0)\n"
        "asyncio.run(main())\n"
    )
    owner_env = dict(os.environ)
    owner_env["PATH"] = f"{tmp_path}{os.pathsep}{owner_env['PATH']}"
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        str(herdr_bin),
        str(tmp_path),
        str(worker_pid_file),
        cwd=str(tmp_path),
        env=owner_env,
    )

    try:
        # Act
        await asyncio.wait_for(owner.wait(), timeout=2.0)
        for _ in range(100):
            if workspace_closed.exists():
                break
            await asyncio.sleep(0.01)

        # Assert
        assert owner.returncode == 0
        assert pane_close_started.exists()
        assert workspace_closed.read_text() == "fake:workspace\n"
    finally:
        if worker_pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(worker_pid_file.read_text()), signal.SIGKILL)


async def test_abrupt_herdr_worker_death_kills_the_target(tmp_path):
    # Arrange
    client = _FakeHerdrClient()
    host = HerdrExecutionHost(client=client)
    lock_file = tmp_path / "target.lock"
    target_pid_file = tmp_path / "target.pid"
    target_script = (
        "import fcntl, os, sys, time\n"
        "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
        "handle = open(sys.argv[1], 'w')\n"
        "fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "time.sleep(60)\n"
    )
    run = await host.launch(
        [sys.executable, "-c", target_script, str(lock_file), str(target_pid_file)],
        cwd=str(tmp_path),
    )
    for _ in range(100):
        if _lock_is_held(str(lock_file)):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("target never acquired its liveness lock")

    # Act
    os.kill(run.pid, signal.SIGKILL)
    with pytest.raises(HerdrUnavailableError):
        await asyncio.wait_for(run.wait(), timeout=1.0)

    # Assert
    for _ in range(100):
        if not _lock_is_held(str(lock_file)):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("target outlived abrupt Herdr worker death")
    run.release()
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)
    if target_pid_file.exists():
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(target_pid_file.read_text()), signal.SIGKILL)


def test_herdr_host_is_available_from_the_package_public_surface():
    # Arrange / Act
    from agent_shell import HerdrExecutionHost as exported_host

    # Assert
    assert exported_host is HerdrExecutionHost


async def test_agentshell_executes_a_claude_run_through_the_herdr_host(
        tmp_path, monkeypatch):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    events = [
        claude_fixtures.SYSTEM_EVENT,
        claude_fixtures.TEXT_EVENT,
        claude_fixtures.RESULT_EVENT_SUCCESS,
    ]
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"for event in {events!r}:\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=HerdrExecutionHost(client=client),
    )

    # Act
    response = await shell.execute(cwd=str(tmp_path), prompt="say hello")

    # Assert
    assert response.response == "Hey! Here's some text output."
    assert response.cost == claude_fixtures.RESULT_EVENT_SUCCESS["total_cost_usd"]
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_agentshell_execute_waits_for_herdr_cleanup_before_returning(
        tmp_path, monkeypatch):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    events = [
        claude_fixtures.SYSTEM_EVENT,
        claude_fixtures.TEXT_EVENT,
        claude_fixtures.RESULT_EVENT_SUCCESS,
    ]
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"for event in {events!r}:\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=HerdrExecutionHost(client=client),
    )

    # Act
    await shell.execute(cwd=str(tmp_path), prompt="say hello")

    # Assert
    assert client.closed_panes == ["fake:pane-1"]


async def test_agentshell_health_check_uses_the_herdr_host(
        tmp_path, monkeypatch):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    events = [
        claude_fixtures.SYSTEM_EVENT,
        claude_fixtures.TEXT_EVENT,
        claude_fixtures.RESULT_EVENT_SUCCESS,
    ]
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"for event in {events!r}:\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=HerdrExecutionHost(client=client),
    )

    # Act
    result = await shell.health_check(cwd=str(tmp_path), model="haiku")

    # Assert
    assert result.healthy is True
    assert result.exception is None
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_agentshell_preserves_herdr_target_nonzero_status(
        tmp_path, monkeypatch):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    events = [
        claude_fixtures.SYSTEM_EVENT,
        claude_fixtures.TEXT_EVENT,
        claude_fixtures.RESULT_EVENT_SUCCESS,
    ]
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"for event in {events!r}:\n"
        "    print(json.dumps(event), flush=True)\n"
        "raise SystemExit(23)\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=HerdrExecutionHost(client=client),
    )

    # Act / Assert
    with pytest.raises(AgentExecutionError) as excinfo:
        await shell.execute(cwd=str(tmp_path), prompt="fail")

    assert excinfo.value.returncode == 23
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)


async def test_agentshell_abandoned_stream_cleans_up_its_herdr_pane(
        tmp_path, monkeypatch):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        f"print(json.dumps({claude_fixtures.SYSTEM_EVENT!r}), flush=True)\n"
        "time.sleep(60)\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=HerdrExecutionHost(client=client),
    )
    stream = shell.stream(cwd=str(tmp_path), prompt="wait")

    # Act
    await anext(stream)
    await stream.aclose()

    # Assert
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)
    assert client.closed_panes == ["fake:pane-1"]


@pytest.mark.parametrize(
    ("agent_type", "binary_name", "events"),
    [
        (
            AgentType.CLAUDE_CODE,
            "claude",
            [
                claude_fixtures.SYSTEM_EVENT,
                claude_fixtures.TEXT_EVENT,
                claude_fixtures.RESULT_EVENT_SUCCESS,
            ],
        ),
        (
            AgentType.OPENCODE,
            "opencode",
            [
                opencode_fixtures.STEP_START_EVENT,
                opencode_fixtures.TEXT_EVENT,
                opencode_fixtures.STEP_FINISH_STOP_EVENT,
            ],
        ),
        (
            AgentType.CODEX,
            "codex",
            [
                codex_fixtures.THREAD_STARTED_EVENT,
                codex_fixtures.AGENT_MESSAGE_COMPLETED_EVENT,
                codex_fixtures.TURN_COMPLETED_EVENT,
            ],
        ),
        (
            AgentType.COPILOT_CLI,
            "copilot",
            [copilot_fixtures.MESSAGE_EVENT_NO_TOOLS, copilot_fixtures.RESULT_EVENT_SUCCESS],
        ),
        (
            AgentType.PI,
            "pi",
            [pi_fixtures.SESSION_EVENT, pi_fixtures.TEXT_END_UPDATE,
             pi_fixtures.AGENT_END_TEXT_EVENT],
        ),
        (
            AgentType.CURSOR,
            "cursor-agent",
            [cursor_fixtures.SYSTEM_INIT_EVENT, cursor_fixtures.ASSISTANT_TEXT_EVENT,
             cursor_fixtures.RESULT_SUCCESS_EVENT],
        ),
        (
            AgentType.GROK,
            "grok",
            [grok_fixtures.SYSTEM_INIT_EVENT, grok_fixtures.ASSISTANT_TEXT_EVENT,
             grok_fixtures.RESULT_SUCCESS_EVENT],
        ),
    ],
)
async def test_all_adapters_execute_through_the_same_herdr_host(
        agent_type, binary_name, events, tmp_path, monkeypatch):
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / binary_name
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"for event in {events!r}:\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = _FakeHerdrClient()
    shell = AgentShell(
        agent_type=agent_type,
        execution_host=HerdrExecutionHost(client=client),
    )

    # Act
    response = await shell.execute(cwd=str(tmp_path), prompt="say hello")

    # Assert
    assert response.response
    await asyncio.wait_for(client.closed.wait(), timeout=1.0)
