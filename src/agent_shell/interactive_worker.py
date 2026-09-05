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


def write_status(directory: Path, value: dict) -> None:
    temporary = directory / "status.tmp"
    temporary.write_text(json.dumps(value))
    temporary.replace(directory / "status.json")


def main() -> None:
    # A Python handler resets to the default on exec, unlike SIG_IGN. Ctrl-C reaches the
    # harness while this supervisor survives to record its exit status.
    signal.signal(signal.SIGINT, lambda *_: None)
    directory = Path(sys.argv[1])
    owner_fd = os.open(directory / "owner", os.O_RDONLY | os.O_NONBLOCK)

    def owner_alive() -> bool:
        try:
            return os.read(owner_fd, 1) != b""
        except BlockingIOError:
            return True

    # Keep the pane alive until the owner has its ID, even if exec will fail immediately.
    deadline = time.monotonic() + 10
    while not (directory / "start").exists():
        if not owner_alive() or time.monotonic() > deadline:
            shutil.rmtree(directory, ignore_errors=True)
            return
        time.sleep(0.02)
    launch = json.loads((directory / "launch.json").read_text())
    (directory / "launch.json").unlink()
    env = launch["env"]
    # tmux supplies these for this pane; the parent may have different terminal coordinates.
    for key in ("TERM", "TMUX", "TMUX_PANE"):
        if key in os.environ:
            env[key] = os.environ[key]
    try:
        child = subprocess.Popen(
            launch["command"], cwd=launch["cwd"], env=env, process_group=0,
        )
    except OSError as error:
        write_status(directory, {"error": str(error)})
    else:
        # Give the harness (and its children) a foreground process group of its own. This
        # preserves /dev/tty and terminal-generated signals while permitting owned cleanup.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(0, child.pid)
        # A fast reader may have received SIGTTIN before the foreground handoff.
        os.killpg(child.pid, signal.SIGCONT)
        write_status(directory, {"pid": child.pid, "returncode": None})
        while child.poll() is None and not (directory / "stop").exists() and owner_alive():
            time.sleep(0.02)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
        returncode = child.wait()
        write_status(directory, {"pid": child.pid, "returncode": returncode})
    # Retain the real screen for inspection until the owner closes the session.
    while owner_alive():
        time.sleep(0.05)
    os.close(owner_fd)
    shutil.rmtree(directory, ignore_errors=True)
    # A user's remain-on-exit setting must not retain an orphaned pane.
    with contextlib.suppress(OSError):
        subprocess.run(["tmux", "kill-pane", "-t", os.environ["TMUX_PANE"]], timeout=2)


if __name__ == "__main__":
    main()
