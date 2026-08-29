"""One-shot command worker for :class:`HerdrExecutionHost`.

The worker is intentionally a tiny stdlib-only process.  It is launched by Herdr with only a
bootstrap module and a private socket path; the command and environment arrive over the framed
socket after the worker has announced its lifecycle PID.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import os
import shlex
import signal
import sys

from agent_shell.herdr_protocol import (
    BRIDGE_CONFIG,
    CANCEL,
    EXIT,
    HELLO,
    LAUNCH,
    LAUNCH_ERROR,
    LAUNCH_READY,
    RELEASE,
    STDERR,
    STDIN,
    STDIN_EOF,
    STDOUT,
    read_frame,
    write_frame,
)

_DEFAULT_CLEANUP_TIMEOUT = 5.0
_MIRRORED_FDS: set[int] = set()


def _install_parent_death_guard() -> None:
    """Arrange for Linux to kill this process if its Herdr owner disappears.

    ``prctl`` is available through libc, so this guard does not add a Python dependency.  The
    second parent-PID read closes the small race where the owner exits between the initial read
    and installing ``PR_SET_PDEATHSIG``.  Other Unix platforms retain normal bridge-disconnect
    cleanup but have no equivalent guard in this stdlib-only worker.
    """
    if not sys.platform.startswith("linux"):
        return
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _mirror_to_pane(payload: bytes, kind: bytes) -> None:
    """Best-effort, non-blocking mirror of target output to Herdr's pane streams.

    The Unix bridge remains the authoritative and lossless output path.  Herdr's inherited
    stdout/stderr are only a convenience view, so a full pipe or an unusable descriptor may
    drop the mirror without affecting the caller's output.
    """
    try:
        descriptor = (
            sys.stdout.fileno() if kind == STDOUT else sys.stderr.fileno()
        )
        if descriptor not in _MIRRORED_FDS:
            os.set_blocking(descriptor, False)
            _MIRRORED_FDS.add(descriptor)
        os.write(descriptor, payload)
    except (BlockingIOError, OSError, ValueError):
        return


def _kill_target(process: asyncio.subprocess.Process) -> None:
    """Kill the target process group without signalling the Herdr worker itself."""
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _close_owned_pane(cleanup_spec: dict | None) -> None:
    """Best-effort cleanup for when the host disappears before it can release the pane."""
    if not cleanup_spec:
        return
    pane_id = cleanup_spec.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        return
    command = cleanup_spec.get("herdr_command") or ["herdr"]
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError:
            return
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        return
    cleanup_timeout = cleanup_spec.get("cleanup_timeout", _DEFAULT_CLEANUP_TIMEOUT)
    if not isinstance(cleanup_timeout, (int, float)) or cleanup_timeout <= 0:
        cleanup_timeout = _DEFAULT_CLEANUP_TIMEOUT
    argv = list(command)
    session = cleanup_spec.get("session")
    if session is not None:
        if not isinstance(session, str) or not session:
            return
        argv.extend(["--session", session])
    argv.extend(["pane", "close", pane_id])
    await _run_cleanup_command(argv, cleanup_timeout)

    workspace_id = cleanup_spec.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return
    workspace_argv = list(command)
    if session is not None:
        workspace_argv.extend(["--session", session])
    workspace_argv.extend(["workspace", "close", workspace_id])
    await _run_cleanup_command(workspace_argv, cleanup_timeout)


async def _run_cleanup_command(argv: list[str], timeout: float) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return
    try:
        await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()


def _remove_bridge_directory(socket_path: str) -> None:
    """Remove only the socket and private directory allocated for this worker."""
    with contextlib.suppress(OSError):
        os.unlink(socket_path)
    with contextlib.suppress(OSError):
        os.rmdir(os.path.dirname(socket_path))


async def _run_worker(socket_path: str) -> int:
    _install_parent_death_guard()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    write_lock = asyncio.Lock()

    async def send(kind: bytes, payload: bytes = b"") -> None:
        async with write_lock:
            await write_frame(writer, kind, payload)

    cleanup_spec = None
    try:
        await send(HELLO, json.dumps({"pid": os.getpid()}).encode("utf-8"))
        kind, payload = await read_frame(reader)
        if kind == BRIDGE_CONFIG:
            cleanup_spec = json.loads(payload.decode("utf-8"))
            kind, payload = await read_frame(reader)
        if kind != LAUNCH:
            raise RuntimeError("Herdr bridge expected a launch request")
        spec = json.loads(payload.decode("utf-8"))
        command = spec["command"]
        cwd = spec["cwd"]
        env = spec.get("env")
        stdin_mode = spec.get("stdin", "devnull")
    except (
        asyncio.IncompleteReadError,
        ConnectionError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
    ):
        await _close_owned_pane(cleanup_spec)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        _remove_bridge_directory(socket_path)
        return 1
    disconnected = asyncio.Event()

    try:
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
            preexec_fn=_install_parent_death_guard,
        )
    except (OSError, TypeError, ValueError) as error:
        await send(
            LAUNCH_ERROR,
            json.dumps(
                {
                    "message": (
                        getattr(error, "strerror", None) or str(error)
                    ),
                    "errno": getattr(error, "errno", None),
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await _close_owned_pane(cleanup_spec)
        return 127

    await send(LAUNCH_READY)

    async def pump(source: asyncio.StreamReader, kind: bytes) -> None:
        try:
            while chunk := await source.read(65536):
                await send(kind, chunk)
                _mirror_to_pane(chunk, kind)
        except (ConnectionError, BrokenPipeError):
            disconnected.set()

    async def control() -> None:
        try:
            while True:
                control_kind, control_payload = await read_frame(reader)
                if control_kind == STDIN and process.stdin is not None:
                    process.stdin.write(control_payload)
                    await process.stdin.drain()
                elif control_kind == STDIN_EOF and process.stdin is not None:
                    process.stdin.close()
                    with contextlib.suppress(Exception):
                        await process.stdin.wait_closed()
                elif control_kind in (CANCEL, RELEASE):
                    _kill_target(process)
                else:
                    raise RuntimeError(
                        f"Herdr bridge received unknown control frame {control_kind!r}"
                    )
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            BrokenPipeError,
            RuntimeError,
        ):
            disconnected.set()
            _kill_target(process)

    stdout_task = asyncio.create_task(pump(process.stdout, STDOUT))
    stderr_task = asyncio.create_task(pump(process.stderr, STDERR))
    control_task = asyncio.create_task(control())

    # If the host disconnects, control() kills the target. If the target exits normally, the
    # control task can be cancelled after its output has drained.
    await process.wait()
    with contextlib.suppress(asyncio.CancelledError):
        control_task.cancel()
        await control_task
    await asyncio.gather(stdout_task, stderr_task)

    try:
        if not disconnected.is_set():
            await send(EXIT, json.dumps({"returncode": process.returncode}).encode("utf-8"))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await _close_owned_pane(cleanup_spec)
        if disconnected.is_set():
            _remove_bridge_directory(socket_path)
    return process.returncode if process.returncode is not None else 255


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(_run_worker(args.socket))


if __name__ == "__main__":
    sys.exit(main())
