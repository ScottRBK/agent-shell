"""One-shot worker used by TerminalWindowExecutionHost.

This module is intentionally a tiny process boundary.  It receives all run data over a
private Unix socket, starts the ordinary NativeExecutionHost, and mirrors each raw output
chunk to the terminal while forwarding the same bytes to its parent.
"""

import argparse
import asyncio
import contextlib
import json
import os
import struct
import sys

from agent_shell.execution import NoIsolation, NativeExecutionHost
from agent_shell.terminal_protocol import (
    _TERMINAL_CANCEL,
    _TERMINAL_ERROR,
    _TERMINAL_FRAME_HEADER,
    _TERMINAL_HELLO,
    _TERMINAL_REQUEST,
    _TERMINAL_STATUS,
    _TERMINAL_STDERR,
    _TERMINAL_STDOUT,
    _TERMINAL_STDIN,
    _TERMINAL_STDIN_EOF,
    _read_terminal_frame,
    _write_terminal_frame,
)


async def _mirror(fd: int, data: bytes) -> None:
    """Best-effort display copy; the private IPC stream remains authoritative."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            # A terminal/PTY that stops consuming output must never stall the target or
            # the lossless IPC stream. The display copy may lose the remainder of this
            # chunk under backpressure.
            return
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]


async def _pump_output(
    stream: asyncio.StreamReader,
    kind: bytes,
    terminal_fd: int,
    writer: asyncio.StreamWriter,
    send_lock: asyncio.Lock,
) -> None:
    try:
        os.set_blocking(terminal_fd, False)
    except OSError:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        async with send_lock:
            await _write_terminal_frame(writer, kind, chunk)
        await _mirror(terminal_fd, chunk)


async def _send_error(
    writer: asyncio.StreamWriter,
    message: str,
    send_lock: asyncio.Lock,
) -> None:
    async with send_lock:
        with contextlib.suppress((ConnectionError, OSError)):
            await _write_terminal_frame(
                writer,
                _TERMINAL_ERROR,
                (message.rstrip() + "\n").encode("utf-8", "replace"),
            )


async def _control_loop(
    reader: asyncio.StreamReader,
    process,
    *,
    writer: asyncio.StreamWriter,
    send_lock: asyncio.Lock,
) -> bool:
    """Return whether cancellation was requested by the parent."""
    try:
        while True:
            kind, payload = await _read_terminal_frame(reader)
            if kind == _TERMINAL_STDIN:
                if process.stdin is not None:
                    process.stdin.write(payload)
                    await process.stdin.drain()
            elif kind == _TERMINAL_STDIN_EOF:
                if process.stdin is not None:
                    process.stdin.close()
                    with contextlib.suppress(Exception):
                        await process.stdin.wait_closed()
            elif kind == _TERMINAL_CANCEL:
                await process.cancel()
                return True
            else:
                await _send_error(writer, f"unknown worker control frame: {kind!r}", send_lock)
    except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        # Parent disappearance is cancellation. NativeRunHandle's guardian then kills the
        # CLI group even if this worker is terminated while handling the exception.
        if process.returncode is None:
            await process.cancel()
        return True


async def _run(
    request: dict,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> int:
    command = request.get("command")
    cwd = request.get("cwd")
    env = request.get("env")
    stdin_mode = request.get("stdin")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) for value in command)
        or not isinstance(cwd, str)
        or (env is not None and not isinstance(env, dict))
        or stdin_mode not in {"devnull", "pipe"}
    ):
        await _send_error(writer, "invalid terminal worker launch request", asyncio.Lock())
        return 255
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in (env or {}).items()
    ):
        await _send_error(writer, "invalid terminal worker environment", asyncio.Lock())
        return 255

    host = NativeExecutionHost()
    try:
        process = await host.launch(
            command,
            cwd=cwd,
            env=env,
            stdin=(asyncio.subprocess.PIPE
                   if stdin_mode == "pipe" else asyncio.subprocess.DEVNULL),
            isolation_policy=NoIsolation(),
        )
    except Exception as error:
        await _send_error(
            writer,
            f"terminal worker could not start command: {error}",
            asyncio.Lock(),
        )
        return 255

    send_lock = asyncio.Lock()
    stdout_task = asyncio.create_task(
        _pump_output(process.stdout, _TERMINAL_STDOUT, sys.stdout.fileno(), writer, send_lock)
    )
    stderr_task = asyncio.create_task(
        _pump_output(process.stderr, _TERMINAL_STDERR, sys.stderr.fileno(), writer, send_lock)
    )
    control_task = asyncio.create_task(
        _control_loop(reader, process, writer=writer, send_lock=send_lock)
    )
    output_task = asyncio.gather(stdout_task, stderr_task)
    process_wait_task = None
    completed_normally = False
    try:
        done, _ = await asyncio.wait(
            (output_task, control_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if control_task in done and control_task.result():
            await output_task
            returncode = await process.wait()
        else:
            await output_task
            # A command may close stdout/stderr before it exits. Keep listening for a
            # cancellation frame while waiting for that command's actual lifecycle status.
            process_wait_task = asyncio.create_task(process.wait())
            done, _ = await asyncio.wait(
                (process_wait_task, control_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if control_task in done:
                control_task.result()
                await process_wait_task
            else:
                control_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await control_task
            returncode = process_wait_task.result()
        await _write_terminal_frame(
            writer,
            _TERMINAL_STATUS,
            struct.pack("!i", returncode),
        )
        completed_normally = True
        return returncode
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        if not output_task.done():
            output_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await output_task
        if process_wait_task is not None and not process_wait_task.done():
            process_wait_task.cancel()
        if process_wait_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await process_wait_task
        if not control_task.done():
            control_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await control_task
        if not completed_normally and process.returncode is None:
            with contextlib.suppress(Exception):
                await process.cancel()
        process.release()


async def _async_main(socket_path: str) -> int:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await _write_terminal_frame(
            writer,
            _TERMINAL_HELLO,
            json.dumps({"pid": os.getpid()}, separators=(",", ":")).encode("utf-8"),
        )
        kind, payload = await _read_terminal_frame(reader)
        if kind != _TERMINAL_REQUEST:
            await _send_error(writer, "terminal worker expected a launch request", asyncio.Lock())
            return 255
        request = json.loads(payload.decode("utf-8"))
        return await _run(request, reader, writer)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(socket_path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    return asyncio.run(_async_main(args.socket))


if __name__ == "__main__":
    raise SystemExit(main())
