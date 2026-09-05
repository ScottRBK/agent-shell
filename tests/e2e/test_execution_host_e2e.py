"""Real-CLI smoke coverage for the opt-in execution boundary.

Local-only: this uses the authenticated Codex CLI and incurs a small gpt-5.4-mini call per mode.
"""

import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import signal
import sys
import time

import pytest

from agent_shell.execution import (
    HerdrExecutionHost,
    IsolationUnavailableError,
    LinuxPidNamespaceIsolation,
    NativeExecutionHost,
)
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell

pytestmark = pytest.mark.e2e


def _lock_is_held(path) -> bool:
    with open(path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return False


def _snapshot_body(response: object) -> dict:
    """Unwrap Herdr's CLI/API result envelope without depending on private client code."""
    if not isinstance(response, dict):
        return {}
    result = response.get("result")
    if isinstance(result, dict):
        response = result
    snapshot = response.get("snapshot")
    if isinstance(snapshot, dict):
        return snapshot
    return response


def _resource_ids(response: dict) -> tuple[set[str], set[str]]:
    body = _snapshot_body(response)
    panes = {
        pane["pane_id"]
        for pane in body.get("panes", [])
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }
    workspaces = {
        workspace["workspace_id"]
        for workspace in body.get("workspaces", [])
        if isinstance(workspace, dict)
        and isinstance(workspace.get("workspace_id"), str)
    }
    return panes, workspaces


async def _herdr_snapshot() -> dict:
    process = await asyncio.create_subprocess_exec(
        "herdr",
        "api",
        "snapshot",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2.0)
    except TimeoutError as error:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise RuntimeError("Herdr snapshot timed out") from error
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Herdr exited with status {process.returncode}")
    response = json.loads(stdout.decode("utf-8"))
    if not isinstance(response, dict):
        raise TypeError("Herdr returned a non-object snapshot")
    return response


async def _herdr_snapshot_or_skip() -> dict:
    """Gate external setup before creating any Herdr-owned resource."""
    if shutil.which("herdr") is None:
        pytest.skip("the optional Herdr executable is not installed")
    if os.environ.get("HERDR_ENV") != "1":
        pytest.skip("real Herdr E2E requires HERDR_ENV=1; refusing to manage this session")
    try:
        return await _herdr_snapshot()
    except (OSError, RuntimeError, TypeError, json.JSONDecodeError) as error:
        pytest.skip(f"Herdr server is unavailable: {error}")


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _wait_for_new_resources(
    baseline: tuple[set[str], set[str]], timeout: float = 5.0
) -> tuple[set[str], set[str]]:
    baseline_panes, baseline_workspaces = baseline
    deadline = time.monotonic() + timeout
    latest = (set(), set())
    while time.monotonic() < deadline:
        panes, workspaces = _resource_ids(await _herdr_snapshot())
        latest = (panes - baseline_panes, workspaces - baseline_workspaces)
        if latest[0] and latest[1]:
            return latest
        await asyncio.sleep(0.05)
    return latest


async def _wait_for_resources_to_disappear(
    baseline: tuple[set[str], set[str]],
    owned: tuple[set[str], set[str]],
    timeout: float = 5.0,
) -> bool:
    baseline_panes, baseline_workspaces = baseline
    owned_panes, owned_workspaces = owned
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        panes, workspaces = _resource_ids(await _herdr_snapshot())
        current_panes = panes - baseline_panes
        current_workspaces = workspaces - baseline_workspaces
        if not (owned_panes & current_panes) and not (
            owned_workspaces & current_workspaces
        ):
            return True
        await asyncio.sleep(0.05)
    panes, workspaces = _resource_ids(await _herdr_snapshot())
    return not (owned_panes & (panes - baseline_panes)) and not (
        owned_workspaces & (workspaces - baseline_workspaces)
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_real_herdr_host_runs_a_local_command(tmp_path):
    # Arrange
    baseline = _resource_ids(await _herdr_snapshot_or_skip())
    host = HerdrExecutionHost()

    # Act
    run = await host.launch(
        [
            sys.executable,
            "-c",
            "import sys; print('herdr ok', flush=True); sys.stdin.buffer.read()",
        ],
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
    )
    try:
        target_pid = run.pid
        owned = await _wait_for_new_resources(baseline)
        stdout, stderr = await run.communicate()
        target_gone = await _wait_until(lambda: not _pid_exists(target_pid))
        run.release()
        await asyncio.wait_for(run.wait_release(), timeout=10.0)
        resources_gone = await _wait_for_resources_to_disappear(baseline, owned)
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run.cancel(), timeout=10.0)
        run.release()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run.wait_release(), timeout=10.0)

    # Assert
    assert stdout == b"herdr ok\n"
    assert stderr == b""
    assert run.returncode == 0
    assert owned[0]
    assert owned[1]
    assert resources_gone
    assert target_gone


async def test_real_herdr_host_cancels_target_and_removes_owned_resources(tmp_path):
    # Arrange
    baseline = _resource_ids(await _herdr_snapshot_or_skip())
    lock_path = tmp_path / "real-herdr-target.lock"
    host = HerdrExecutionHost()
    run = await host.launch(
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

    # Act
    try:
        target_started = await _wait_until(lambda: _lock_is_held(lock_path))
        owned = await _wait_for_new_resources(baseline)
        await asyncio.wait_for(run.cancel(), timeout=10.0)
        target_stopped = await _wait_until(lambda: not _lock_is_held(lock_path))
        resources_gone = await _wait_for_resources_to_disappear(baseline, owned)
        target_gone = await _wait_until(lambda: not _pid_exists(target_pid))
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run.cancel(), timeout=10.0)
        run.release()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run.wait_release(), timeout=10.0)

    # Assert
    assert target_started
    assert owned[0]
    assert owned[1]
    assert run.returncode == -signal.SIGKILL
    assert target_stopped
    assert resources_gone
    assert target_gone


@pytest.mark.parametrize(
    "mount_proc",
    [
        pytest.param(True, id="private-proc"),
        pytest.param(False, id="inherited-proc"),
    ],
)
async def test_real_codex_health_check_completes_inside_pid_isolation(tmp_path, mount_proc):
    # Arrange
    policy = LinuxPidNamespaceIsolation(mount_proc=mount_proc)
    host = NativeExecutionHost()
    try:
        probe = await host.launch(
            [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps({"
                "'pid': os.getpid(), 'proc_pid': int(os.readlink('/proc/self'))}))",
            ],
            cwd=str(tmp_path),
            isolation_policy=policy,
        )
    except IsolationUnavailableError as error:
        pytest.skip(str(error))
    try:
        stdout, stderr = await asyncio.wait_for(probe.communicate(), timeout=5.0)
    finally:
        await probe.cancel()
    assert probe.returncode == 0, stderr.decode()
    namespace = json.loads(stdout)
    assert namespace["pid"] == 2
    assert (namespace["proc_pid"] == 2) is mount_proc

    shell = AgentShell(
        agent_type=AgentType.CODEX,
        isolation_policy=policy,
    )

    # Act
    result = await shell.health_check(cwd=str(tmp_path), model="gpt-5.4-mini", timeout=60.0)

    # Assert
    assert result.healthy is True, result.exception
    assert result.exception is None
