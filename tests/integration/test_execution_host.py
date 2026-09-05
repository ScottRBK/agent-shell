"""Public execution-host behaviour with real local processes."""

import asyncio
import json
import os
import shutil
import signal
import sys
import uuid

import pytest

from agent_shell import execution as execution_module
from agent_shell.execution import NativeExecutionHost
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


class _BlockingStream:
    async def read(self, _size=-1):
        await asyncio.Future()


class _RecordingRunHandle:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.stdin = None
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()
        self.cancelled = False
        self.released = False

    async def wait(self):
        return self.returncode

    async def communicate(self, input=None):
        del input
        return b"", b""

    async def cancel(self):
        self.cancelled = True
        self.returncode = -signal.SIGKILL

    def release(self):
        self.released = True


class _RecordingExecutionHost:
    def __init__(self):
        self.run = _RecordingRunHandle()
        self.launched = asyncio.Event()

    async def launch(self, command, cwd, **kwargs):
        del command, cwd, kwargs
        self.launched.set()
        return self.run


class _ProbeResult:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


@pytest.mark.parametrize("agent_type", list(AgentType))
async def test_cancelling_a_stream_uses_the_selected_hosts_run_handle(agent_type, tmp_path):
    # Arrange
    host = _RecordingExecutionHost()
    shell = AgentShell(agent_type=agent_type, execution_host=host)
    next_event = asyncio.create_task(
        anext(shell.stream(cwd=str(tmp_path), prompt="wait until cancelled"))
    )
    await asyncio.wait_for(host.launched.wait(), timeout=1.0)

    # Act
    next_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_event

    # Assert
    assert host.run.cancelled is True
    assert host.run.released is False


async def test_native_host_launches_a_real_process_without_extra_configuration(tmp_path):
    # Arrange
    host = NativeExecutionHost()
    command = [
        sys.executable,
        "-c",
        "import sys; print('native stdout'); print('native stderr', file=sys.stderr)",
    ]

    # Act
    run = await host.launch(command, cwd=str(tmp_path))
    stdout = await run.stdout.read()
    stderr = await run.stderr.read()
    returncode = await run.wait()
    run.release()

    # Assert
    assert stdout == b"native stdout\n"
    assert stderr == b"native stderr\n"
    assert returncode == 0
    assert run.returncode == 0


@pytest.mark.parametrize(
    ("mount_proc", "expected_options"),
    [
        (
            None,
            ("--user", "--map-current-user", "--pid", "--fork", "--mount-proc"),
        ),
        (
            "environment",
            ("--user", "--map-current-user", "--pid", "--fork", "--mount-proc"),
        ),
        ("explicit-over-environment", ("--user", "--map-current-user", "--pid", "--fork")),
        (
            True,
            ("--user", "--map-current-user", "--pid", "--fork", "--mount-proc"),
        ),
        (False, ("--user", "--map-current-user", "--pid", "--fork")),
    ],
)
async def test_pid_isolation_mount_proc_controls_probe_and_launch_command(
        monkeypatch, mount_proc, expected_options):
    # Arrange
    monkeypatch.setattr(execution_module.sys, "platform", "linux")
    monkeypatch.setattr(execution_module.shutil, "which", lambda _: "/usr/bin/unshare")
    probe_calls = []

    async def record_probe(*args, **kwargs):
        probe_calls.append((args, kwargs))
        return _ProbeResult()

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_exec",
        record_probe,
    )
    if isinstance(mount_proc, str):
        monkeypatch.setenv("AGENTSHELL_ISOLATION_POLICY", "linux-pid-namespace")
        kwargs = {}
        if mount_proc == "explicit-over-environment":
            kwargs["isolation_policy"] = execution_module.LinuxPidNamespaceIsolation(
                mount_proc=False,
            )
        policy = AgentShell(agent_type=AgentType.CLAUDE_CODE, **kwargs).isolation_policy
    else:
        policy = (
            execution_module.LinuxPidNamespaceIsolation()
            if mount_proc is None
            else execution_module.LinuxPidNamespaceIsolation(mount_proc=mount_proc)
        )
    command = [sys.executable, "-c", "pass"]

    first = second = None
    try:
        # Act
        first = await policy.prepare(command, None)
        second = await policy.prepare(command, None)

        # Assert
        expected_probe = ["/usr/bin/unshare", *expected_options, "true"]
        assert list(probe_calls[0][0]) == expected_probe
        assert len(probe_calls) == 1

        expected_launch_prefix = [
            "/usr/bin/unshare",
            *expected_options,
            sys.executable,
            "-I",
            "-S",
            "-c",
        ]
        assert first.command[:len(expected_launch_prefix)] == expected_launch_prefix
        assert first.command[-len(command):] == command
        assert second.command == first.command
    finally:
        if first is not None:
            first.failed()
        if second is not None:
            second.failed()


@pytest.mark.parametrize("value", [None, 0, 1, "true", object()])
def test_pid_isolation_mount_proc_requires_a_bool(value):
    # Arrange / Act / Assert
    with pytest.raises(TypeError, match="mount_proc"):
        execution_module.LinuxPidNamespaceIsolation(mount_proc=value)


async def test_pid_isolation_probe_is_mode_specific_and_fails_closed(
        monkeypatch, tmp_path):
    # Arrange
    monkeypatch.setattr(execution_module.sys, "platform", "linux")
    monkeypatch.setattr(execution_module.shutil, "which", lambda _: "/usr/bin/unshare")
    probe_calls = []

    async def mode_specific_probe(*args, **kwargs):
        probe_calls.append((args, kwargs))
        if "--mount-proc" in args:
            return _ProbeResult(
                returncode=1,
                stderr=b"unshare: operation not permitted with --mount-proc",
            )
        return _ProbeResult()

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_exec",
        mode_specific_probe,
    )
    policy_without_proc = execution_module.LinuxPidNamespaceIsolation(mount_proc=False)
    policy_with_proc = execution_module.LinuxPidNamespaceIsolation(mount_proc=True)
    host = NativeExecutionHost()

    prepared = None
    try:
        # Act
        prepared = await policy_without_proc.prepare(
            [sys.executable, "-c", "pass"],
            None,
        )
        with pytest.raises(
            execution_module.IsolationUnavailableError,
            match="operation not permitted with --mount-proc",
        ):
            await host.launch(
                [sys.executable, "-c", "raise SystemExit(88)"],
                cwd=str(tmp_path),
                isolation_policy=policy_with_proc,
            )

        # Assert
        assert list(probe_calls[0][0]) == [
            "/usr/bin/unshare",
            "--user",
            "--map-current-user",
            "--pid",
            "--fork",
            "true",
        ]
        assert list(probe_calls[1][0]) == [
            "/usr/bin/unshare",
            "--user",
            "--map-current-user",
            "--pid",
            "--fork",
            "--mount-proc",
            "true",
        ]
    finally:
        if prepared is not None:
            prepared.failed()


@pytest.mark.parametrize(
    ("mount_proc", "expected_options"),
    [
        (
            True,
            ("--user", "--map-current-user", "--pid", "--fork", "--mount-proc"),
        ),
        (False, ("--user", "--map-current-user", "--pid", "--fork")),
    ],
)
async def test_pid_isolation_fails_closed_when_requested_mode_is_unavailable(
        monkeypatch, tmp_path, mount_proc, expected_options):
    # Arrange
    monkeypatch.setattr(execution_module.sys, "platform", "linux")
    monkeypatch.setattr(execution_module.shutil, "which", lambda _: "/usr/bin/unshare")
    probe_calls = []

    async def reject_probe(*args, **kwargs):
        probe_calls.append((args, kwargs))
        return _ProbeResult(
            returncode=1,
            stderr=b"unshare: operation not permitted",
        )

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_exec",
        reject_probe,
    )
    policy = execution_module.LinuxPidNamespaceIsolation(mount_proc=mount_proc)
    host = NativeExecutionHost()

    # Act / Assert
    with pytest.raises(
        execution_module.IsolationUnavailableError,
        match="operation not permitted",
    ):
        await host.launch(
            [sys.executable, "-c", "raise SystemExit(88)"],
            cwd=str(tmp_path),
            isolation_policy=policy,
        )

    assert list(probe_calls[0][0]) == [
        "/usr/bin/unshare",
        *expected_options,
        "true",
    ]


@pytest.mark.parametrize("mount_proc", [True, False])
async def test_pid_isolation_proc_visibility_matches_the_requested_mode(tmp_path, mount_proc):
    # Arrange
    host = NativeExecutionHost()
    policy = execution_module.LinuxPidNamespaceIsolation(mount_proc=mount_proc)

    # Act
    try:
        run = await host.launch(
            [
                sys.executable,
                "-c",
                "import json, os, sys; print(json.dumps({"
                "'pid': os.getpid(), 'proc_pid': int(os.readlink('/proc/self')), "
                "'owner_visible': os.path.exists('/proc/' + sys.argv[1])}))",
                str(os.getpid()),
            ],
            cwd=str(tmp_path),
            isolation_policy=policy,
        )
    except execution_module.IsolationUnavailableError as error:
        pytest.skip(str(error))
    try:
        stdout, stderr = await asyncio.wait_for(run.communicate(), timeout=5)
    finally:
        await run.cancel()

    # Assert
    assert run.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert result["pid"] == 2
    assert result["owner_visible"] is (not mount_proc)
    assert (result["proc_pid"] == 2) is mount_proc


async def test_pid_isolation_hides_the_owner_from_a_broad_cleanup_command(tmp_path):
    # Arrange — both the owner and its child carry the same unique argv marker. Without a
    # child PID namespace, `pkill -f` can see and terminate both of them.
    if shutil.which("pkill") is None:
        pytest.skip("pkill is required for the PID-isolation integration test")

    marker = f"agentshell-isolation-{uuid.uuid4().hex}"
    child_script = (
        "import subprocess, sys, time\n"
        "subprocess.run(['pkill', '-TERM', '-f', sys.argv[1]], check=False)\n"
        "time.sleep(2)\n"
    )
    owner_script = (
        "import asyncio, json, os, sys\n"
        "from agent_shell.execution import (\n"
        "    IsolationUnavailableError, LinuxPidNamespaceIsolation, NativeExecutionHost,\n"
        ")\n"
        "async def main():\n"
        "    host = NativeExecutionHost()\n"
        "    try:\n"
        "        run = await host.launch(\n"
        f"            [sys.executable, '-c', {child_script!r}, sys.argv[1]],\n"
        "            cwd=sys.argv[2],\n"
        "            isolation_policy=LinuxPidNamespaceIsolation(),\n"
        "        )\n"
        "    except IsolationUnavailableError as error:\n"
        "        print(json.dumps({'unavailable': str(error)}), flush=True)\n"
        "        return\n"
        "    stdout = await run.stdout.read()\n"
        "    stderr = await run.stderr.read()\n"
        "    returncode = await run.wait()\n"
        "    run.release()\n"
        "    print(json.dumps({\n"
        "        'returncode': returncode,\n"
        "        'reported_returncode': run.returncode,\n"
        "        'stdout': stdout.decode(),\n"
        "        'stderr': stderr.decode(),\n"
        "    }), flush=True)\n"
        "asyncio.run(main())\n"
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        marker,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tmp_path),
    )

    # Act
    owner_stdout, owner_stderr = await asyncio.wait_for(owner.communicate(), timeout=10.0)

    # Assert
    assert owner.returncode == 0, owner_stderr.decode()
    result = json.loads(owner_stdout.decode().strip())
    if "unavailable" in result:
        pytest.skip(result["unavailable"])
    assert result["returncode"] == -signal.SIGTERM
    assert result["reported_returncode"] == -signal.SIGTERM


@pytest.mark.parametrize("mount_proc", [True, False])
@pytest.mark.parametrize(
    ("script", "expected_status"),
    [
        ("raise SystemExit(0)", 0),
        ("raise SystemExit(37)", 37),
        ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", -signal.SIGTERM),
    ],
)
async def test_pid_isolation_preserves_exit_and_signal_status(
        tmp_path, mount_proc, script, expected_status):
    # Arrange
    host = NativeExecutionHost()

    # Act
    try:
        run = await host.launch(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            isolation_policy=execution_module.LinuxPidNamespaceIsolation(mount_proc=mount_proc),
        )
    except execution_module.IsolationUnavailableError as error:
        pytest.skip(str(error))
    await run.stdout.read()
    await run.stderr.read()
    returncode = await run.wait()
    run.release()

    # Assert
    assert returncode == expected_status
    assert run.returncode == expected_status


async def test_requested_pid_isolation_fails_instead_of_falling_back(
        monkeypatch, tmp_path):
    # Arrange — simulate a non-Linux kernel at the public host boundary. The command would
    # leave a marker if it were silently launched without the requested protection.
    marker = tmp_path / "must-not-run"
    monkeypatch.setattr(execution_module.sys, "platform", "darwin")
    host = NativeExecutionHost()
    policy = execution_module.LinuxPidNamespaceIsolation()

    # Act / Assert
    with pytest.raises(
        execution_module.IsolationUnavailableError,
        match="only available on Linux",
    ):
        await host.launch(
            [sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()",
             str(marker)],
            cwd=str(tmp_path),
            isolation_policy=policy,
        )

    assert not marker.exists()


async def test_pid_isolation_reports_an_unshare_start_failure_clearly(
        monkeypatch, tmp_path):
    # Arrange — `unshare` was found, but the operating system refuses to start it.
    monkeypatch.setattr(execution_module.sys, "platform", "linux")
    monkeypatch.setattr(execution_module.shutil, "which", lambda _: "/usr/bin/unshare")

    async def refuse_spawn(*args, **kwargs):
        raise OSError("execution denied")

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_exec",
        refuse_spawn,
    )
    host = NativeExecutionHost()

    # Act / Assert
    with pytest.raises(
        execution_module.IsolationUnavailableError,
        match="could not start `unshare`: execution denied",
    ):
        await host.launch(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            isolation_policy=execution_module.LinuxPidNamespaceIsolation(),
        )


async def test_agentshell_survives_a_matching_cleanup_command(tmp_path):
    # Arrange — install a real fake Claude CLI whose broad cleanup pattern also appears in
    # its AgentShell owner's argv. This is issue #17's black-box reproduction.
    if shutil.which("pkill") is None:
        pytest.skip("pkill is required for the PID-isolation integration test")

    marker = f"agentshell-facade-isolation-{uuid.uuid4().hex}"
    bin_dir = tmp_path / marker / "bin"
    bin_dir.mkdir(parents=True)
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, time\n"
        "subprocess.run(['pkill', '-TERM', '-f', os.environ['AGENTSHELL_TEST_MARKER']], "
        "check=False)\n"
        "time.sleep(2)\n"
    )
    fake_claude.chmod(0o755)
    owner_script = (
        "import asyncio, json, sys\n"
        "from agent_shell.execution import (\n"
        "    IsolationUnavailableError, LinuxPidNamespaceIsolation, NativeExecutionHost,\n"
        ")\n"
        "from agent_shell.models.agent import AgentExecutionError, AgentType\n"
        "from agent_shell.shell import AgentShell\n"
        "async def main():\n"
        "    shell = AgentShell(\n"
        "        agent_type=AgentType.CLAUDE_CODE,\n"
        "        execution_host=NativeExecutionHost(),\n"
        "        isolation_policy=LinuxPidNamespaceIsolation(),\n"
        "    )\n"
        "    try:\n"
        "        await shell.execute(cwd=sys.argv[2], prompt='run cleanup')\n"
        "    except IsolationUnavailableError as error:\n"
        "        print(json.dumps({'unavailable': str(error)}), flush=True)\n"
        "    except AgentExecutionError as error:\n"
        "        print(json.dumps({\n"
        "            'reason': str(error),\n"
        "            'returncode': error.returncode,\n"
        "            'signal': error.signal,\n"
        "        }), flush=True)\n"
        "asyncio.run(main())\n"
    )
    env = dict(execution_module.os.environ)
    env["PATH"] = f"{bin_dir}{execution_module.os.pathsep}{env['PATH']}"
    env["AGENTSHELL_TEST_MARKER"] = marker
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        marker,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tmp_path),
        env=env,
    )

    # Act
    owner_stdout, owner_stderr = await asyncio.wait_for(owner.communicate(), timeout=10.0)

    # Assert
    assert owner.returncode == 0, owner_stderr.decode()
    result = json.loads(owner_stdout.decode().strip())
    if "unavailable" in result:
        pytest.skip(result["unavailable"])
    assert result == {
        "reason": "process terminated by signal SIGTERM (15)",
        "returncode": -signal.SIGTERM,
        "signal": signal.SIGTERM,
    }
