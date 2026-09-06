"""Process-group ownership and cleanup through dedicated guardian processes.

Every CLI runs in a process group containing a tiny guardian. AgentShell owns the guardian through
an anonymous pipe, which is an exact kernel object rather than a reusable numeric PID. Cleanup sends
one byte through that pipe; the guardian then signals its own group.

A normal stream sends RELEASE so a CLI's intentional leftover processes may continue. Cancellation,
abandonment, model-discovery cleanup, and atexit send KILL. If AgentShell disappears before cleanup,
the pipe closes and the guardian treats EOF as KILL.

The parent never calls ``killpg``. If the guardian has already died, writing its pipe fails safely
and cleanup accepts a possible leak rather than risking a signal to a recycled process-group ID.
"""
import asyncio
import atexit
import inspect
from typing import Protocol

from agent_shell.process_guardian import (
    _GroupGuardian,
    _KILL_GROUP,
    _RELEASE_GROUP,
    _send_guardian_command,
    _start_guardian,
)

# Run handles have stable identity even after their numeric PIDs are reaped and reused.
_guardians: dict[object, _GroupGuardian] = {}


class _ManagedRun(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def cancel(self) -> None: ...

    def release(self) -> None: ...


def transfer_process_guardian(process: object, run_handle: object) -> None:
    """Move exact guardian ownership from a raw process to its public run handle."""
    guardian = _guardians.pop(process)
    _guardians[run_handle] = guardian


async def create_grouped_process(
    command: list[str],
    cwd: str,
    *,
    env: dict[str, str] | None = None,
    stdin: int = asyncio.subprocess.DEVNULL,
    pass_fds: tuple[int, ...] = (),
) -> asyncio.subprocess.Process:
    """Start a command in a process group owned by an exact guardian pipe."""
    guardian = _start_guardian()
    try:
        spawn_options = {
            "stdin": stdin,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": cwd,
            "env": env,
            "process_group": guardian.pid,
        }
        if pass_fds:
            spawn_options["pass_fds"] = pass_fds
        process = await asyncio.create_subprocess_exec(*command, **spawn_options)
    except BaseException:
        _send_guardian_command(guardian, _KILL_GROUP)
        raise

    _guardians[process] = guardian
    return process


def release_process_group(process: object) -> None:
    """Stop the guardian without signalling leftovers from a completed CLI."""
    guardian = _guardians.pop(process, None)
    if guardian is not None:
        _send_guardian_command(guardian, _RELEASE_GROUP)


def kill_process_group(process: object) -> None:
    """Ask the process's exact guardian to SIGKILL its own group."""
    guardian = _guardians.pop(process, None)
    if guardian is not None:
        _send_guardian_command(guardian, _KILL_GROUP)


async def release_process(
    process: _ManagedRun,
    active_processes: list,
    stderr_task,
    *,
    child_exited: bool,
) -> None:
    """Release the resources owned by one adapter stream on every exit path."""
    if not stderr_task.done():
        stderr_task.cancel()

    if process not in active_processes:
        return
    active_processes.remove(process)

    if child_exited or process.returncode is not None:
        process.release()
        wait_release = getattr(process, "wait_release", None)
        if wait_release is not None:
            result = wait_release()
            if inspect.isawaitable(result):
                await result
    else:
        await process.cancel()


def cleanup_process_groups() -> None:
    """Ask every still-registered guardian to kill its group during interpreter exit."""
    for process in list(_guardians):
        kill_process_group(process)


atexit.register(cleanup_process_groups)
