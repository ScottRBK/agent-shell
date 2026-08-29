"""Private worker used by :class:`TmuxExecutionHost`.

The worker is deliberately a small stdlib-only process.  Its terminal is owned by tmux, while
the actual CLI gets ordinary pipes so the host can preserve the separate stdout/stderr streams
expected by the adapters.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import struct
import sys

from agent_shell.tmux_protocol import (
    CANCEL,
    CLOSE_STDIN,
    CONFIG,
    ERROR,
    EXIT,
    HELLO,
    RELEASE,
    STDERR,
    STDIN,
    STDOUT,
    receive_frame,
    send_frame,
)


async def _mirror(stream, payload: bytes) -> None:
    """Best-effort mirror to the pane; IPC remains the authoritative transport."""
    try:
        # tmux owns a PTY here.  A pane that is not being viewed can fill its output queue, and
        # blocking this write would stop the bridge from forwarding the authoritative socket
        # stream.  Set the descriptor non-blocking once it is first used and drop only the pane
        # mirror when it would block; the host-side framed stream remains lossless.
        descriptor = stream.fileno()
        os.set_blocking(descriptor, False)
        try:
            os.write(descriptor, payload)
        except BlockingIOError:
            pass
    except (BrokenPipeError, OSError):
        pass


async def _forward_output(
    source: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    channel: int,
    pane_stream,
    send_lock: asyncio.Lock,
) -> None:
    while True:
        payload = await source.read(65536)
        if not payload:
            return
        async with send_lock:
            await send_frame(writer, channel, payload)
        await _mirror(pane_stream, payload)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, 9)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


def _set_parent_death_signal() -> None:
    """Make the target follow the bridge if tmux tears down its pane unexpectedly."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        # Linux prctl(PR_SET_PDEATHSIG, SIGKILL).  This is best-effort: the normal control EOF
        # path still kills the target group, while ctypes keeps the bridge stdlib-only.
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)
    except (AttributeError, OSError):
        pass


async def _control_input(
    reader: asyncio.StreamReader,
    process: asyncio.subprocess.Process,
    stdin_mode: str,
    released: asyncio.Event,
) -> None:
    try:
        while True:
            channel, payload = await receive_frame(reader)
            if channel == RELEASE:
                # Releasing an active handle is cleanup, not permission for a target to outlive
                # its pane.  The host also kills the exact tmux session, but terminate the target
                # group here first so the process cannot briefly survive a slow tmux response.
                await _kill_process_group(process)
                released.set()
                return
            if channel == CANCEL:
                await _kill_process_group(process)
                continue
            if channel == STDIN and stdin_mode == "pipe" and process.stdin is not None:
                process.stdin.write(payload)
                await process.stdin.drain()
            elif channel == CLOSE_STDIN and stdin_mode == "pipe" and process.stdin is not None:
                process.stdin.close()
    except (asyncio.IncompleteReadError, ConnectionError):
        # The owner disappeared without a release.  Wake the run coordinator so it can kill and
        # reap the target rather than waiting forever for a frame that cannot arrive.
        released.set()
        raise


async def run(socket_path: str) -> int:
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    process: asyncio.subprocess.Process | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        await send_frame(writer, HELLO, str(os.getpid()).encode("ascii"))

        channel, payload = await receive_frame(reader)
        if channel != CONFIG:
            raise RuntimeError("tmux bridge expected launch configuration")
        config = json.loads(payload.decode("utf-8"))
        command = [str(item) for item in config["command"]]
        cwd = str(config["cwd"])
        env = {str(key): str(value) for key, value in config["env"].items()}

        stdin_mode = str(config.get("stdin", "devnull"))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_mode == "pipe"
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_set_parent_death_signal if os.name == "posix" else None,
        )
        send_lock = asyncio.Lock()
        released = asyncio.Event()
        control_task = asyncio.create_task(
            _control_input(reader, process, stdin_mode, released)
        )
        stdout_task = asyncio.create_task(
            _forward_output(process.stdout, writer, STDOUT, sys.stdout, send_lock)
        )
        stderr_task = asyncio.create_task(
            _forward_output(process.stderr, writer, STDERR, sys.stderr, send_lock)
        )
        process_wait_task = asyncio.create_task(process.wait())
        try:
            # A live control connection is part of the run's ownership boundary.  If the host
            # disappears before sending RELEASE, do not wait forever for a target that has lost
            # its owner; close the exact process group and then reap it.
            done, _ = await asyncio.wait(
                (control_task, process_wait_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if control_task in done:
                control_error = control_task.exception()
                if control_error is not None and not isinstance(
                    control_error, (asyncio.IncompleteReadError, ConnectionError)
                ):
                    raise control_error
                if process.returncode is None:
                    await _kill_process_group(process)

            returncode = await process_wait_task
            # A child may retain inherited pipe descriptors after the CLI itself exits.  The
            # pane is disposable in v1, so bound the drain and let handle release remove it.
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task), timeout=5.0
                )
            except TimeoutError:
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            async with send_lock:
                await send_frame(writer, EXIT, struct.pack("!i", returncode))
            await released.wait()
        finally:
            for task in (control_task, stdout_task, stderr_task, process_wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                control_task, stdout_task, stderr_task, process_wait_task,
                return_exceptions=True,
            )
    except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
        if process is not None:
            await _kill_process_group(process)
        return 1
    except BaseException as error:  # noqa: BLE001 - clean up the target on worker failure
        if process is not None:
            await _kill_process_group(process)
        if writer is not None:
            with contextlib.suppress(Exception):
                await send_frame(writer, ERROR, str(error).encode("utf-8", errors="replace"))
        return 1
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.socket)))


if __name__ == "__main__":
    main()
