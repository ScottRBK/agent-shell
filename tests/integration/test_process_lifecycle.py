"""End-to-end process lifecycle with real children, guardians, pipes, and signals.

The fake CLIs avoid network calls, but process creation and cleanup are real. Tests observe death
through process status and kernel-held locks rather than mocked signal calls.
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

from agent_shell import process_cleanup
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter
from agent_shell.adapters.grok_adapter import GrokAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.pi_adapter import PiAdapter
from agent_shell.execution import (
    IsolationUnavailableError,
    LinuxPidNamespaceIsolation,
    NativeExecutionHost,
)
from agent_shell.models.agent import AgentExecutionError, AgentType
from agent_shell.process_cleanup import (
    cleanup_process_groups,
    kill_process_group,
)
from agent_shell.shell import AgentShell
from tests.unit.adapter_matrix import ADAPTERS, OK_RESULT_EVENT

# Adapter class to the public selector used by AgentShell.
AGENT_TYPE = {
    ClaudeCodeAdapter: AgentType.CLAUDE_CODE,
    CodexAdapter: AgentType.CODEX,
    OpenCodeAdapter: AgentType.OPENCODE,
    CopilotCLIAdapter: AgentType.COPILOT_CLI,
    PiAdapter: AgentType.PI,
    CursorAdapter: AgentType.CURSOR,
    GrokAdapter: AgentType.GROK,
}

# The executable name each adapter puts at argv[0].
CLI_NAME = {
    ClaudeCodeAdapter: "claude",
    CodexAdapter: "codex",
    OpenCodeAdapter: "opencode",
    CopilotCLIAdapter: "copilot",
    PiAdapter: "pi",
    CursorAdapter: "cursor-agent",
    GrokAdapter: "grok",
}

# Reads its script from the environment so one file serves every adapter and every scenario.
# The grandchild is spawned before any output, so a consumer that has seen one event knows the
# grandchild exists. Tests that need the host to observe its lifecycle use the lock file rather
# than the PID: an isolated child's PID is meaningful only inside its PID namespace.
#
# With `grandchild_lock_file` the grandchild holds an exclusive flock for its whole life
# instead of just sleeping. The kernel releases that lock when the last descriptor on it
# closes, which for a SIGKILLed process is at exit, so a test can observe the grandchild's
# death directly rather than inferring it from a pid that may have been recycled.
#
# The grandchild gets DEVNULL for all three streams rather than inheriting the child's pipes.
# Unless explicitly detached, it stays in the guardian-owned group. It never holds the stdout and
# stderr open — and asyncio
# resolves `Process.wait()` from `_call_connection_lost`, which `_try_finish` only reaches once
# `all(p.disconnected ...)`. Inheriting the pipes therefore gated the child's `wait()` on the
# grandchild's 60s lifetime, which is why awaiting the child used to be a trap. On DEVNULL it
# is a 0ms reap barrier instead, and no test has to poll for the reap.
_FAKE_CLI = '''#!/usr/bin/env python3
import json, os, subprocess, sys, time
spec = json.loads(os.environ["AGENTSHELL_FAKE_CLI"])
if spec.get("required_pid") is not None and os.getpid() != spec["required_pid"]:
    sys.exit(88)
if spec.get("grandchild_pid_file") or spec.get("grandchild_lock_file"):
    if spec.get("grandchild_lock_file"):
        argv = [sys.executable, "-c",
                "import fcntl, sys, time\\n"
                "f = open(sys.argv[1], 'w')\\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\\n"
                "time.sleep(60)\\n",
                spec["grandchild_lock_file"]]
    else:
        argv = ["sleep", "60"]
    grandchild = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=spec.get("grandchild_detached", False),
    )
    if spec.get("grandchild_pid_file"):
        with open(spec["grandchild_pid_file"], "w") as f:
            f.write(str(grandchild.pid))
if spec.get("sleep_before_stdout"):
    time.sleep(spec["sleep_before_stdout"])
for event in spec["stdout"]:
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()
for output in spec.get("raw_stdout", []):
    sys.stdout.write(output)
    sys.stdout.flush()
if spec.get("exit_gate"):
    while not os.path.exists(spec["exit_gate"]):
        time.sleep(0.01)
if spec.get("hang"):
    time.sleep(60)
if spec.get("terminate_signal"):
    os.kill(os.getpid(), spec["terminate_signal"])
sys.exit(spec.get("exit_code", 0))
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


# Both polling helpers below are async, and `await asyncio.sleep()` rather than `time.sleep()`
# is the whole point of them being so: they are called from async tests, so a `time.sleep()` poll
# would block the very event loop that has to make progress for the condition to come true.
#
# Reaping was the case that bit, and it is worth recording because nothing about the test looked
# environment-dependent. asyncio picks its child watcher by capability, not by version:
# `_UnixDefaultEventLoopPolicy._init_watcher()` installs PidfdChildWatcher when `os.pidfd_open`
# exists and falls back to ThreadedChildWatcher when it does not, and the two reap in different
# places. ThreadedChildWatcher runs `os.waitpid()` on a thread of its own, so it reaps whatever
# the loop is doing. PidfdChildWatcher registers the pidfd with `loop._add_reader()` and runs
# `os.waitpid()` from the reader callback, so it reaps only while the loop is turning. Under the
# pidfd watcher a blocking poll therefore held the child at `/proc/<pid>/stat` state Z for the
# full timeout, with `os.getpgid()` still succeeding and `returncode` still None, and no amount
# of extra timeout would have helped. Reproduced: the same suite passed on a
# python-build-standalone interpreter (no `os.pidfd_open`) and failed on every interpreter built
# with pidfd support.
#
# The reap itself is now awaited directly rather than polled — see the barrier in
# `test_cleanup_kills_the_grandchildren_of_an_already_reaped_child`. What is left below is the
# waiting that genuinely has no barrier to await, because it is another process reaching a state
# of its own. Keep it cooperative anyway: these run under whichever watcher the interpreter
# picked, and a loop this test blocks is a loop that cannot deliver anything.
async def _reaped(pid: int, timeout: float = 5.0) -> bool:
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
        await asyncio.sleep(0.01)
    return False


def _copilot_rpc_frame(message: dict) -> str:
    payload = json.dumps(message, separators=(",", ":"))
    return f"Content-Length: {len(payload.encode('utf-8'))}\r\n\r\n{payload}"


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """True once `predicate` holds; False if it never does within `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
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


async def test_normal_release_stops_guardian_without_killing_leftovers(tmp_path):
    # Arrange — a successful CLI may intentionally leave a server running. Releasing its
    # guardian must preserve that existing behaviour rather than killing the whole group.
    lock_file = tmp_path / "released-grandchild.lock"
    grandchild_script = (
        "import fcntl, sys, time\n"
        "f = open(sys.argv[1], 'w')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        "time.sleep(60)\n"
    )
    script = (
        "import subprocess, sys\n"
        f"grandchild_script = {grandchild_script!r}\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', grandchild_script, sys.argv[1]],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "print(child.pid, flush=True)\n"
    )
    process = await process_cleanup.create_grouped_process(
        [sys.executable, "-c", script, str(lock_file)],
        cwd=str(tmp_path),
    )
    grandchild_pid = int((await process.stdout.readline()).strip())

    try:
        assert await _wait_until(lambda: _lock_is_held(str(lock_file)))
        await process.wait()

        # Act
        process_cleanup.release_process_group(process)

        # Assert
        assert _lock_is_held(str(lock_file)), "normal release killed a leftover process"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)
        await _wait_until(lambda: not _lock_is_held(str(lock_file)))


async def test_guardian_does_not_inherit_the_owners_file_descriptors(tmp_path):
    # Arrange — make one descriptor explicitly inheritable to cover FDs opened outside Python's
    # normal CLOEXEC defaults, as a native extension might do.
    inherited_path = tmp_path / "must-not-be-inherited"
    inherited_fd = os.open(inherited_path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.set_inheritable(inherited_fd, True)
    process = await process_cleanup.create_grouped_process(
        ["sleep", "60"],
        cwd=str(tmp_path),
    )
    guardian = process_cleanup._guardians[process]

    try:
        # Act — /proc is a live view: an fd can close after listdir() but before readlink().
        # That normal Linux race made the tag build flaky with FileNotFoundError. Ignore only
        # entries that vanished; any descriptor still open is captured and asserted below.
        guardian_fds: dict[str, str] = {}
        for fd in os.listdir(f"/proc/{guardian.pid}/fd"):
            try:
                guardian_fds[fd] = os.readlink(f"/proc/{guardian.pid}/fd/{fd}")
            except FileNotFoundError:
                continue

        # Assert — a guardian must not keep an owner's output pipe, log, or native FD open.
        assert guardian_fds["1"] == "/dev/null"
        assert guardian_fds["2"] == "/dev/null"
        assert str(inherited_path) not in guardian_fds.values()
    finally:
        os.close(inherited_fd)
        kill_process_group(process)
        await process.wait()


async def test_dead_guardian_fails_safe_without_signalling_by_group_number(tmp_path):
    # Arrange — if the exact guardian has died, its pipe is the only trustworthy ownership
    # handle. Cleanup must accept a leak rather than fall back to a potentially recycled PGID.
    process = await process_cleanup.create_grouped_process(
        ["sleep", "60"],
        cwd=str(tmp_path),
    )
    guardian = process_cleanup._guardians[process]
    guardian.process.kill()
    guardian.process.wait()

    try:
        # Act
        kill_process_group(process)
        await asyncio.sleep(0.05)

        # Assert
        assert process.returncode is None, "cleanup signalled the group after ownership was lost"
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def test_guardian_kills_group_when_owner_interpreter_disappears(tmp_path):
    # Arrange — run AgentShell in a separate interpreter and use os._exit() to bypass both
    # async-generator cleanup and atexit. Closing the private pipe must still trigger cleanup.
    lock_file = tmp_path / "interpreter-exit.lock"
    ready_file = tmp_path / "interpreter-exit.ready"
    child_pid_file = tmp_path / "interpreter-exit-child.pid"
    grandchild_pid_file = tmp_path / "interpreter-exit-grandchild.pid"
    grandchild_script = (
        "import fcntl, os, sys, time\n"
        "f = open(sys.argv[1], 'w')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        "open(sys.argv[2], 'w').close()\n"
        "open(sys.argv[3], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    cli_script = (
        "import subprocess, sys, time\n"
        f"grandchild_script = {grandchild_script!r}\n"
        "subprocess.Popen(\n"
        "    [sys.executable, '-c', grandchild_script, *sys.argv[1:]],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "time.sleep(60)\n"
    )
    owner_script = (
        "import asyncio, os, sys\n"
        "from agent_shell.process_cleanup import create_grouped_process\n"
        "async def main():\n"
        f"    cli_script = {cli_script!r}\n"
        "    process = await create_grouped_process(\n"
        "        [sys.executable, '-c', cli_script, *sys.argv[1:4]],\n"
        "        cwd=os.getcwd(),\n"
        "    )\n"
        "    open(sys.argv[4], 'w').write(str(process.pid))\n"
        "    while not os.path.exists(sys.argv[2]):\n"
        "        await asyncio.sleep(0.01)\n"
        "    os._exit(0)\n"
        "asyncio.run(main())\n"
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_script,
        str(lock_file),
        str(ready_file),
        str(grandchild_pid_file),
        str(child_pid_file),
        cwd=os.getcwd(),
    )

    child_pid = None
    grandchild_pid = None
    try:
        # Act
        await asyncio.wait_for(owner.wait(), timeout=5.0)
        child_pid = int(child_pid_file.read_text())
        grandchild_pid = int(grandchild_pid_file.read_text())

        # Assert
        assert ready_file.exists(), "grandchild was never confirmed alive"
        assert await _wait_until(lambda: not _lock_is_held(str(lock_file)))
        assert await _reaped(child_pid)
        assert await _reaped(grandchild_pid)
    finally:
        for pid in (child_pid, grandchild_pid):
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


async def test_owned_group_is_killed_through_its_guardian(tmp_path):
    # Arrange — exercise the real ownership mechanism with a child and grandchild. The lock is
    # independent evidence that the grandchild is alive; losing it proves the whole group died.
    lock_file = tmp_path / "guardian-grandchild.lock"
    grandchild_script = (
        "import fcntl, sys, time\n"
        "f = open(sys.argv[1], 'w')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        "time.sleep(60)\n"
    )
    script = (
        "import subprocess, sys, time\n"
        f"grandchild_script = {grandchild_script!r}\n"
        "subprocess.Popen(\n"
        "    [sys.executable, '-c', grandchild_script, sys.argv[1]],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "time.sleep(60)\n"
    )
    process = await process_cleanup.create_grouped_process(
        [sys.executable, "-c", script, str(lock_file)],
        cwd=str(tmp_path),
    )

    try:
        assert await _wait_until(lambda: _lock_is_held(str(lock_file))), (
            "grandchild never took the lock that marks it alive"
        )

        # Act
        kill_process_group(process)
        await asyncio.wait_for(process.wait(), timeout=5.0)

        # Assert
        assert process.returncode == -signal.SIGKILL
        assert await _wait_until(lambda: not _lock_is_held(str(lock_file)))
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_completed_stream_leaves_nothing_registered(
        adapter_cls, fake_cli, tmp_path):
    # Arrange — normal completion must release the exact guardian handle after reaping the CLI.
    adapter = adapter_cls()
    fake_cli(stdout=[OK_RESULT_EVENT[adapter_cls]])

    # Act
    child = None
    events = []
    async for event in adapter.stream(cwd=str(tmp_path), prompt="ping"):
        if child is None:
            child = adapter._active_processes[0]
        events.append(event)

    # Assert
    assert any(e.type == "result" for e in events), "fake CLI did not drive a full run"
    assert child.returncode is not None, "child was not reaped on the normal path"
    assert adapter._active_processes == []
    assert child not in process_cleanup._guardians


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
@pytest.mark.parametrize(
    ("termination", "expected_returncode", "expected_signal", "expected_reason"),
    [
        (
            {"terminate_signal": signal.SIGTERM},
            -signal.SIGTERM,
            signal.SIGTERM,
            "process terminated by signal SIGTERM (15)",
        ),
        (
            {"exit_code": 23},
            23,
            None,
            "process exited with code 23",
        ),
    ],
    ids=["signal", "exit-code"],
)
async def test_process_termination_is_reported_structurally(
        adapter_cls, termination, expected_returncode, expected_signal,
        expected_reason, fake_cli, tmp_path):
    # Arrange — the CLI emits a valid result and then terminates unsuccessfully without
    # writing stderr. The process result must still overrule the success-shaped event.
    shell = AgentShell(agent_type=AGENT_TYPE[adapter_cls])
    fake_cli(stdout=[OK_RESULT_EVENT[adapter_cls]], **termination)

    # Act
    with pytest.raises(AgentExecutionError) as excinfo:
        await shell.execute(cwd=str(tmp_path), prompt="ping")

    # Assert
    error = excinfo.value
    assert str(error) == expected_reason
    assert error.returncode == expected_returncode
    assert error.signal == expected_signal


async def test_stream_exposes_process_termination_metadata(fake_cli, tmp_path):
    # Arrange
    shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
    fake_cli(
        stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]],
        terminate_signal=signal.SIGTERM,
    )

    # Act
    events = [
        event
        async for event in shell.stream(cwd=str(tmp_path), prompt="ping")
    ]

    # Assert
    error = next(event for event in events if event.type == "error")
    assert error.content == "process terminated by signal SIGTERM (15)"
    assert error.returncode == -signal.SIGTERM
    assert error.signal == signal.SIGTERM


async def test_agentshell_applies_the_requested_pid_isolation(fake_cli, tmp_path):
    # Arrange — the namespace init is PID 1, so the real CLI must be PID 2. This observable
    # requirement fails if AgentShell merely stores the policy without wiring it to adapters.
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=NativeExecutionHost(),
        isolation_policy=LinuxPidNamespaceIsolation(),
    )
    fake_cli(stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]], required_pid=2)

    # Act
    try:
        response = await shell.execute(cwd=str(tmp_path), prompt="ping")
    except IsolationUnavailableError as error:
        pytest.skip(str(error))

    # Assert
    assert response.response == ""


async def test_agentshell_applies_pid_isolation_selected_by_environment(
        fake_cli, tmp_path, monkeypatch):
    # Arrange — exercise the deployment-level configuration through the public AgentShell
    # boundary. Namespace PID 2 proves the selected policy reached the real CLI process.
    monkeypatch.setenv(
        "AGENTSHELL_ISOLATION_POLICY",
        "linux-pid-namespace",
    )
    shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
    fake_cli(stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]], required_pid=2)

    # Act
    try:
        response = await shell.execute(cwd=str(tmp_path), prompt="ping")
    except IsolationUnavailableError as error:
        pytest.skip(str(error))

    # Assert
    assert response.response == ""


@pytest.mark.parametrize("mount_proc", [True, False])
@pytest.mark.parametrize("ending", ["normal", "cancel", "timeout"])
async def test_pid_isolation_cleans_up_a_detached_child(
        fake_cli, tmp_path, mount_proc, ending):
    # Arrange — prove the detached child holds a kernel lock before ending its namespace.
    # Observing lock release avoids interpreting namespace-local PIDs on the host.
    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        execution_host=NativeExecutionHost(),
        isolation_policy=LinuxPidNamespaceIsolation(mount_proc=mount_proc),
    )
    lock_file = tmp_path / "grandchild.lock"
    exit_gate = tmp_path / "exit"
    fake_cli(
        stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]],
        exit_gate=str(exit_gate),
        grandchild_lock_file=str(lock_file),
        grandchild_detached=True,
        required_pid=2,
    )

    async def consume_stream():
        async for _ in shell.stream(cwd=str(tmp_path), prompt="ping"):
            pass

    task = asyncio.create_task(consume_stream())
    try:
        ready = await _wait_until(
            lambda: lock_file.exists() and _lock_is_held(str(lock_file))
        )
        if not ready:
            if task.done():
                error = task.exception()
                if isinstance(error, IsolationUnavailableError):
                    pytest.skip(str(error))
            pytest.fail("fake CLI grandchild never acquired its liveness lock")

        # Act
        if ending == "normal":
            exit_gate.touch()
            await asyncio.wait_for(task, timeout=5)
        elif ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=0.05)

        # Assert
        assert await _wait_until(
            lambda: not _lock_is_held(str(lock_file))
        ), f"{ending} left its detached grandchild running"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_cancelled_agent_shell_stream_kills_the_real_process_tree(
        adapter_cls, fake_cli, tmp_path):
    # Arrange — drive cancellation through the public AgentShell boundary. The CLI publishes its
    # grandchild before emitting a result and then hangs, so cancellation has a real tree to stop.
    shell = AgentShell(agent_type=AGENT_TYPE[adapter_cls])
    pid_file = tmp_path / "cancelled-grandchild.pid"
    fake_cli(
        stdout=[OK_RESULT_EVENT[adapter_cls]],
        hang=True,
        grandchild_pid_file=str(pid_file),
    )

    async def consume_stream():
        async for _ in shell.stream(cwd=str(tmp_path), prompt="ping"):
            pass

    task = asyncio.create_task(consume_stream())
    grandchild_pid = None
    try:
        assert await _wait_until(pid_file.exists), "fake CLI never started its grandchild"
        grandchild_pid = int(pid_file.read_text())

        # Act
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Assert
        assert await _reaped(grandchild_pid), "cancelled stream left its grandchild running"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if grandchild_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild_pid, signal.SIGKILL)


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
async def test_abandoned_stream_really_kills_the_child_and_its_grandchild(
        adapter_cls, fake_cli, tmp_path):
    # Arrange — the other half of the contract, and the guard against over-correcting B1 into
    # never killing anything: a consumer that walks away from a live child must take down the
    # guardian-owned group, grandchildren included. Real processes and signals are used here.
    adapter = adapter_cls()
    pid_file = tmp_path / "grandchild.pid"
    fake_cli(stdout=[OK_RESULT_EVENT[adapter_cls]], hang=True,
             grandchild_pid_file=str(pid_file))

    agen = adapter.stream(cwd=str(tmp_path), prompt="ping")
    await agen.__anext__()
    child = adapter._active_processes[0]
    grandchild_pid = int(pid_file.read_text())
    assert child.returncode is None, "child exited before the abandonment under test"
    assert child in process_cleanup._guardians

    # Act — aclose() is what CPython eventually runs for a consumer that `break`s out of the
    # `async for`; calling it directly removes the scheduling delay described below.
    await agen.aclose()

    # Assert
    await child.wait()
    assert child.returncode != 0, "abandoned child was not killed"
    assert await _reaped(grandchild_pid), "grandchild outlived the process group kill"
    assert adapter._active_processes == []
    assert child not in process_cleanup._guardians


async def test_cleanup_kills_the_grandchildren_of_an_already_reaped_child(fake_cli, tmp_path):
    # Arrange — the CLI exits and is reaped while its grandchild remains in the group. The
    # guardian pipe must retain exact ownership after the CLI's numeric PID becomes reusable.
    adapter = ClaudeCodeAdapter()
    pid_file = tmp_path / "grandchild.pid"
    lock_file = tmp_path / "grandchild.lock"
    fake_cli(stdout=[OK_RESULT_EVENT[ClaudeCodeAdapter]],
             grandchild_pid_file=str(pid_file), grandchild_lock_file=str(lock_file))

    agen = adapter.stream(cwd=str(tmp_path), prompt="ping")
    await agen.__anext__()
    child = adapter._active_processes[0]
    grandchild_pid = int(pid_file.read_text())
    try:
        # The lock being held is the positive control: it proves the grandchild is alive and
        # that losing the lock later can only mean it died.
        assert await _wait_until(lambda: _lock_is_held(str(lock_file))), \
            "grandchild never took the lock that marks it alive"

        # A barrier, not a poll. Both watchers call os.waitpid() — the call that frees the pid —
        # before scheduling the callback that resolves wait(), so once this returns the reap has
        # happened and `_pid_is_gone` needs no polling at all. It is only sound because the
        # grandchild is spawned on DEVNULL: asyncio resolves wait() from _call_connection_lost,
        # which _try_finish reaches only once every pipe is disconnected, so a grandchild holding
        # the child's stdout would gate wait() on ITS 60s lifetime instead of the child's.
        # Bounded so that regression fails in seconds rather than hanging the run.
        await asyncio.wait_for(child.wait(), timeout=5.0)
        assert _pid_is_gone(child.pid), "child was never reaped"
        assert child in process_cleanup._guardians

        # Act
        cleanup_process_groups()

        # Assert
        assert await _wait_until(lambda: not _lock_is_held(str(lock_file))), \
            "grandchild outlived the cleanup: it is still holding its lock"
        assert await _reaped(grandchild_pid)
        assert child not in process_cleanup._guardians
    finally:
        # Cleanup — the grandchild must never survive this test, however it ended.
        with contextlib.suppress(OSError):
            os.kill(grandchild_pid, signal.SIGKILL)
        await agen.aclose()


@pytest.mark.parametrize(
    "agent_type",
    [
        pytest.param(AgentType.OPENCODE, id="shared-command-helper"),
        pytest.param(AgentType.COPILOT_CLI, id="copilot-json-rpc"),
    ],
)
async def test_model_discovery_timeout_kills_the_cli_and_its_grandchild(
        agent_type, fake_cli, tmp_path):
    # Arrange
    shell = AgentShell(agent_type=agent_type)
    pid_file = tmp_path / "discovery-grandchild.pid"
    fake_cli(stdout=[], hang=True, grandchild_pid_file=str(pid_file))
    grandchild_pid = None

    try:
        # Act
        with pytest.raises(RuntimeError, match="timed out"):
            await shell.list_models(cwd=str(tmp_path), timeout=0.2)
        grandchild_pid = int(pid_file.read_text())

        # Assert
        assert await _reaped(grandchild_pid, timeout=1.0), (
            "model discovery left its CLI grandchild running"
        )
    finally:
        if grandchild_pid is None and pid_file.exists():
            grandchild_pid = int(pid_file.read_text())
        if grandchild_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(grandchild_pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "agent_type",
    [
        pytest.param(AgentType.OPENCODE, id="shared-command-helper"),
        pytest.param(AgentType.COPILOT_CLI, id="copilot-json-rpc"),
    ],
)
async def test_successful_model_discovery_kills_a_leftover_grandchild(
        agent_type, fake_cli, tmp_path, monkeypatch):
    # Arrange
    shell = AgentShell(agent_type=agent_type)
    pid_file = tmp_path / "successful-discovery-grandchild.pid"
    spec = {
        "stdout": [],
        "grandchild_pid_file": str(pid_file),
    }
    if agent_type == AgentType.OPENCODE:
        spec["raw_stdout"] = ["provider/model\n"]
    else:
        spec["raw_stdout"] = [
            _copilot_rpc_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
            _copilot_rpc_frame({
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"models": [{"id": "model"}]},
            }),
        ]
        spec["sleep_before_stdout"] = 0.05
    fake_cli(**spec)
    grandchild_pid = None

    def kill_only_an_owned_group(process):
        assert process in process_cleanup._guardians, (
            "successful discovery lost its exact guardian ownership handle"
        )
        kill_process_group(process)

    monkeypatch.setattr(
        "agent_shell.adapters.model_discovery.kill_process_group",
        kill_only_an_owned_group,
    )

    try:
        # Act
        models = await shell.list_models(cwd=str(tmp_path))
        grandchild_pid = int(pid_file.read_text())

        # Assert
        assert models
        assert await _reaped(grandchild_pid, timeout=1.0), (
            "successful model discovery left its CLI grandchild running"
        )
    finally:
        if grandchild_pid is None and pid_file.exists():
            grandchild_pid = int(pid_file.read_text())
        if grandchild_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(grandchild_pid, signal.SIGKILL)


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

    # Act
    async for event in adapter.stream(cwd=str(tmp_path), prompt="ping"):
        if event.type == "result":
            break
    child = adapter._active_processes[0]
    still_registered_right_after_break = child in process_cleanup._guardians

    # Assert — teardown has NOT happened yet...
    assert still_registered_right_after_break, "teardown ran synchronously at the break"
    assert child.returncode is None

    # ...and lands once the loop gets a turn.
    for _ in range(100):
        if child not in process_cleanup._guardians:
            break
        await asyncio.sleep(0.01)
    assert child not in process_cleanup._guardians, "deferred teardown never ran"

    # Cleanup
    await child.wait()
