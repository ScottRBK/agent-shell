"""Standalone tmux worker: the child inherits the pane's actual controlling terminal.

Unlike the headless bridge, this worker never pipes or renders the harness's output.
Only startup/exit status travels through private files. Kept stdlib-only for direct execution.
"""

import json
import os
from pathlib import Path
import subprocess
import signal
import contextlib
import shutil
import sys
import time

from tmux_ownership import TmuxResource
from process_guardian import _STOP_GROUP, _send_guardian_command, _start_guardian


def write_status(directory: Path, value: dict) -> None:
    temporary = directory / "status.tmp"
    temporary.write_text(json.dumps(value))
    temporary.replace(directory / "status.json")


def main() -> None:
    # A Python handler resets to the default on exec, unlike SIG_IGN. Ctrl-C reaches the
    # harness while this supervisor survives to record its exit status.
    signal.signal(signal.SIGINT, lambda *_: None)
    directory = Path(sys.argv[1])
    owner_fd = None
    resource = TmuxResource(
        "pane", "", "", os.environ["TMUX_PANE"],
        os.environ["TMUX"].rsplit(",", 2)[0], directory.name,
    )
    try:
        owner_fd = os.open(directory / "owner", os.O_RDONLY | os.O_NONBLOCK)
        run(directory, owner_fd)
    finally:
        if owner_fd is not None:
            os.close(owner_fd)
        shutil.rmtree(directory, ignore_errors=True)
        # Cover startup too: remain-on-exit must never retain an orphaned pane. The socket
        # and launch marker prevent cleanup from reaching another server or a reused ID.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(["tmux", *resource.cleanup_args()], timeout=2)


def run(directory: Path, owner_fd: int) -> None:
    def owner_alive() -> bool:
        try:
            return os.read(owner_fd, 1) != b""
        except BlockingIOError:
            return True

    # Keep the pane alive until the owner has its ID, even if exec will fail immediately.
    deadline = time.monotonic() + 10
    while not (directory / "start").exists():
        if not owner_alive() or time.monotonic() > deadline:
            return
        time.sleep(0.02)
    launch = json.loads((directory / "launch.json").read_text())
    (directory / "launch.json").unlink()
    env = launch["env"]
    # tmux supplies these for this pane; the parent may have different terminal coordinates.
    for key in ("TERM", "TMUX", "TMUX_PANE"):
        if key in os.environ:
            env[key] = os.environ[key]
    guardian = _start_guardian(grace_period=0.5)
    child = None
    try:
        try:
            child = subprocess.Popen(
                launch["command"], cwd=launch["cwd"], env=env, process_group=guardian.pid,
            )
        except OSError as error:
            write_status(directory, {"error": str(error)})
            while owner_alive():
                time.sleep(0.02)
            return
        # The harness and guardian share a foreground group. Only the guardian signals its
        # own group, so cleanup never sends a signal to a recycled numeric process-group ID.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(0, guardian.pid)
        os.write(guardian.control_fd, b"C")  # Resume a fast reader stopped before the handoff.
        write_status(directory, {"pid": child.pid, "returncode": None})
        while child.poll() is None and not (directory / "stop").exists() and owner_alive():
            time.sleep(0.02)
        if child.returncode is not None:
            write_status(directory, {"pid": child.pid, "returncode": child.returncode})
        # Retain the real screen and ownership of any helpers until the owner closes.
        while owner_alive() and not (directory / "stop").exists():
            time.sleep(0.02)
    finally:
        # The grace period is independent of whether the CLI leader has already exited.
        _send_guardian_command(guardian, _STOP_GROUP)
        if child is not None:
            returncode = child.wait()
            with contextlib.suppress(FileNotFoundError):
                write_status(directory, {
                    "pid": child.pid, "returncode": returncode, "stopped": True,
                })

    # Let the controller consume the final status before removing the private directory.
    while owner_alive():
        time.sleep(0.02)


if __name__ == "__main__":
    main()
