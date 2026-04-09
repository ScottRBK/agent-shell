"""
Module-level process group registry with atexit cleanup.

Safety net for orphaned child processes when the parent exits without
calling cancel() — e.g. when asyncio.run() converts SIGINT into
CancelledError and the KeyboardInterrupt handler never fires.

Both adapters register child process group IDs here on subprocess
creation and unregister them on normal completion or explicit cancel().
If any PGIDs remain at interpreter shutdown, atexit kills them.
"""
import atexit
import os
import signal

_active_process_groups: set[int] = set()


def register_process_group(pgid: int) -> None:
    _active_process_groups.add(pgid)


def unregister_process_group(pgid: int) -> None:
    _active_process_groups.discard(pgid)


def cleanup_process_groups() -> None:
    """Kill all registered process groups. Called by atexit."""
    for pgid in list(_active_process_groups):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _active_process_groups.clear()


atexit.register(cleanup_process_groups)
