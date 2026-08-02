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
import contextlib
import logging
import os
import subprocess
import sys
from dataclasses import dataclass


logger = logging.getLogger("agent_shell.process_cleanup")

_KILL_GROUP = b"K"
_RELEASE_GROUP = b"R"

_GROUP_GUARDIAN = """
import os
import signal

command = os.read(0, 1)
if command == b"R":
    raise SystemExit(0)
os.kill(0, signal.SIGKILL)
"""


@dataclass(slots=True)
class _GroupGuardian:
    process: subprocess.Popen
    control_fd: int

    @property
    def pid(self) -> int:
        return self.process.pid


# Process objects have stable identity even after their numeric PIDs are reaped and reused.
_guardians: dict[asyncio.subprocess.Process, _GroupGuardian] = {}


def _send_guardian_command(guardian: _GroupGuardian, command: bytes) -> None:
    try:
        os.write(guardian.control_fd, command)
    except OSError as error:
        logger.warning("Could not contact process-group guardian: %s", error)
    finally:
        with contextlib.suppress(OSError):
            os.close(guardian.control_fd)

    # subprocess.Popen, rather than asyncio, owns this direct child. Waiting here reaps that
    # exact child; the PID is never used to choose a process or group to signal.
    with contextlib.suppress(OSError, ChildProcessError):
        guardian.process.wait()


def _start_guardian() -> _GroupGuardian:
    read_fd, write_fd = os.pipe()
    argv = [sys.executable, "-I", "-S", "-c", _GROUP_GUARDIAN]

    try:
        process = subprocess.Popen(
            argv,
            stdin=read_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            process_group=0,
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)

    return _GroupGuardian(process=process, control_fd=write_fd)


async def create_grouped_process(
    command: list[str],
    cwd: str,
    *,
    env: dict[str, str] | None = None,
    stdin: int = asyncio.subprocess.DEVNULL,
) -> asyncio.subprocess.Process:
    """Start a command in a process group owned by an exact guardian pipe."""
    guardian = _start_guardian()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            process_group=guardian.pid,
        )
    except BaseException:
        _send_guardian_command(guardian, _KILL_GROUP)
        raise

    _guardians[process] = guardian
    return process


def release_process_group(process: asyncio.subprocess.Process) -> None:
    """Stop the guardian without signalling leftovers from a completed CLI."""
    guardian = _guardians.pop(process, None)
    if guardian is not None:
        _send_guardian_command(guardian, _RELEASE_GROUP)


def kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Ask the process's exact guardian to SIGKILL its own group."""
    guardian = _guardians.pop(process, None)
    if guardian is not None:
        _send_guardian_command(guardian, _KILL_GROUP)


def release_process(
    process: asyncio.subprocess.Process,
    active_processes: list,
    stderr_task,
    *,
    child_exited: bool,
) -> None:
    """Release the resources owned by one adapter stream on every exit path."""
    if not stderr_task.done():
        stderr_task.cancel()

    if process in active_processes:
        active_processes.remove(process)

    if child_exited or process.returncode is not None:
        release_process_group(process)
    else:
        kill_process_group(process)


def cleanup_process_groups() -> None:
    """Ask every still-registered guardian to kill its group during interpreter exit."""
    for process in list(_guardians):
        kill_process_group(process)


atexit.register(cleanup_process_groups)
