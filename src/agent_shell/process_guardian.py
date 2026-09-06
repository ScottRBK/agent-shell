"""Stdlib process-group guardian shared by native runs and standalone terminal workers."""

import contextlib
from dataclasses import dataclass
import logging
import os
import subprocess
import sys

logger = logging.getLogger("agent_shell.process_cleanup")

_KILL_GROUP = b"K"
_RELEASE_GROUP = b"R"
_STOP_GROUP = b"S"

_GROUP_GUARDIAN = """
import os
import signal
import sys
import time

# Only the guardian ignores these signals. The CLI is a sibling with its own dispositions.
for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP,
               signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
    signal.signal(signum, signal.SIG_IGN)
os.write(1, b"R")
os.dup2(2, 1)  # Restore /dev/null and close the readiness pipe before the parent proceeds.
grace_period = float(sys.argv[1])
while True:
    command = os.read(0, 1)
    if command == b"C":
        os.kill(0, signal.SIGCONT)
        continue
    if command == b"R":
        raise SystemExit(0)
    if command != b"K" and grace_period:
        os.kill(0, signal.SIGCONT)
        os.kill(0, signal.SIGTERM)
        time.sleep(grace_period)
    os.kill(0, signal.SIGKILL)
"""


@dataclass(slots=True)
class _GroupGuardian:
    process: subprocess.Popen
    control_fd: int

    @property
    def pid(self) -> int:
        return self.process.pid


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


def _start_guardian(*, grace_period: float = 0.0) -> _GroupGuardian:
    read_fd, write_fd = os.pipe()
    argv = [sys.executable, "-I", "-S", "-c", _GROUP_GUARDIAN, str(grace_period)]

    try:
        process = subprocess.Popen(
            argv,
            stdin=read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            process_group=0,
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)

    guardian = _GroupGuardian(process=process, control_fd=write_fd)
    try:
        # Do not launch a CLI until the guardian can survive terminal signals.
        if process.stdout.read() != b"R":
            raise OSError("Process-group guardian failed to start")
    except BaseException:
        _send_guardian_command(guardian, _KILL_GROUP)
        raise
    finally:
        process.stdout.close()
    return guardian
