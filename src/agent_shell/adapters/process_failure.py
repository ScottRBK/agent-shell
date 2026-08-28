"""Normalize raw subprocess termination into the shared stream contract."""

import signal as signal_module

from agent_shell.adapters.stderr_format import format_stderr
from agent_shell.models.agent import StreamEvent


def process_failure_event(
        returncode: int | None,
        stderr: bytes,
) -> StreamEvent | None:
    """Return one structured error event for an unsuccessful process exit."""
    if returncode in (None, 0):
        return None

    signal_number = -returncode if returncode < 0 else None
    reason = format_stderr(stderr)
    if not reason and signal_number is not None:
        try:
            signal_name = signal_module.Signals(signal_number).name
        except ValueError:
            signal_name = "UNKNOWN"
        reason = (
            f"process terminated by signal {signal_name} ({signal_number})"
        )
    elif not reason:
        reason = f"process exited with code {returncode}"

    return StreamEvent(
        type="error",
        content=reason,
        returncode=returncode,
        signal=signal_number,
    )
