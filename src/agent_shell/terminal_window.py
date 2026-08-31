"""Visible terminal-window execution host and its per-run transport.

The launcher boundary is injectable so platform-specific terminal services remain outside
AgentShell's shared execution abstractions.
"""

import asyncio
import atexit
import contextlib
import json
import os
import shutil
import struct
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from agent_shell.execution import (
    IsolationPolicy,
    IsolationUnavailableError,
    NoIsolation,
)
from agent_shell.terminal_protocol import (
    _TERMINAL_CANCEL,
    _TERMINAL_ERROR,
    _TERMINAL_FRAME_HEADER,
    _TERMINAL_HELLO,
    _TERMINAL_MAX_FRAME,
    _TERMINAL_REQUEST,
    _TERMINAL_STATUS,
    _TERMINAL_STDERR,
    _TERMINAL_STDIN,
    _TERMINAL_STDIN_EOF,
    _TERMINAL_STDOUT,
    _read_terminal_frame,
    _write_terminal_frame,
)


class TerminalWindowUnavailableError(RuntimeError):
    """Raised when a visible terminal window cannot be started."""


class TerminalWindowLauncher(Protocol):
    """External terminal-launcher boundary used by TerminalWindowExecutionHost."""

    async def launch(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
    ): ...


class SubprocessTerminalLauncher:
    """Launch a worker command through one configured terminal executable.

    ``command_prefix`` and ``command_option`` are deliberately explicit rather than a
    shell template.  This keeps worker arguments separate and prevents accidental shell
    interpolation.  The returned process is the launcher process owned by one run.
    """

    def __init__(
        self,
        executable: str,
        *,
        command_prefix: Sequence[str] = (),
        command_option: str = "-e",
        display: str = "any",
    ):
        self.executable = executable
        self.command_prefix = tuple(command_prefix)
        self.command_option = command_option
        self.display = display
        self.requires_graphical = True

    async def launch(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
    ):
        argv = [
            self.executable,
            *self.command_prefix,
            self.command_option,
            *command,
        ]
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise TerminalWindowUnavailableError(
                f"could not start terminal launcher {self.executable!r}: {error}"
            ) from error


def discover_terminal_launcher() -> SubprocessTerminalLauncher:
    """Return a usable built-in launcher strategy, or raise a clear error.

    The built-in list intentionally stays small.  Other terminal emulators can be used by
    injecting ``SubprocessTerminalLauncher`` with their documented argument convention.
    """
    configured = os.environ.get("AGENTSHELL_TERMINAL_LAUNCHER")
    if configured is not None:
        configured = configured.strip()
        if not configured:
            raise TerminalWindowUnavailableError(
                "AGENTSHELL_TERMINAL_LAUNCHER is empty"
            )
        executable = shutil.which(configured)
        if executable is None:
            raise TerminalWindowUnavailableError(
                f"configured terminal launcher {configured!r} was not found"
            )
        return SubprocessTerminalLauncher(executable, display="any")

    candidates = (
        ("x-terminal-emulator", (), "-e", "x11"),
        ("gnome-terminal", (), "--", "any"),
        ("konsole", (), "-e", "any"),
        ("kitty", (), "--", "any"),
        ("alacritty", (), "-e", "any"),
        ("foot", (), "--", "any"),
        ("wezterm", ("start",), "--", "any"),
        ("xterm", (), "-e", "x11"),
    )
    for name, prefix, option, display in candidates:
        executable = shutil.which(name)
        if executable is not None:
            return SubprocessTerminalLauncher(
                executable,
                command_prefix=prefix,
                command_option=option,
                display=display,
            )
    raise TerminalWindowUnavailableError(
        "no supported terminal launcher found; install one or inject a "
        "TerminalWindowLauncher strategy"
    )


def _terminal_graphical_session_available(display: str = "any") -> bool:
    if display == "x11":
        return bool(os.environ.get("DISPLAY"))
    if os.environ.get("DISPLAY"):
        return True
    wayland = os.environ.get("WAYLAND_DISPLAY")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return bool(
        wayland
        and runtime
        and Path(runtime, wayland).is_socket()
    )


_TERMINAL_LAUNCHER_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XAUTHORITY",
        "LANG",
        "LANGUAGE",
        "TERM",
        "COLORTERM",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_DESKTOP",
        "VIRTUAL_ENV",
    }
)


def _terminal_launcher_environment() -> dict[str, str]:
    """Keep the launcher environment to desktop plumbing, never run data."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _TERMINAL_LAUNCHER_ENVIRONMENT_KEYS
    }


class _TerminalStdin:
    """Small StreamWriter-shaped facade backed by framed worker IPC."""

    def __init__(self, run: "TerminalWindowRunHandle"):
        self._run = run
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("terminal run stdin is closed")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("terminal run stdin expects bytes")
        # Native asyncio accepts input even when a fast target has already closed its stdin;
        # the worker may have closed the transport by the time communicate() sends that input.
        if self._run.returncode is not None:
            return
        with contextlib.suppress(RuntimeError):
            self._run._write_control_nowait(_TERMINAL_STDIN, bytes(data))

    async def drain(self) -> None:
        await self._run._drain_control()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            with contextlib.suppress(RuntimeError):
                self._run._write_control_nowait(_TERMINAL_STDIN_EOF)

    def is_closing(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        await self._run._drain_control()


_TERMINAL_ACTIVE_RUNS: set["TerminalWindowRunHandle"] = set()


class TerminalWindowRunHandle:
    """RunHandle backed by a one-shot worker in a visible terminal window."""

    def __init__(
        self,
        *,
        worker_pid: int,
        launcher_process,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        socket_dir: str,
        socket_path: str,
        stdin_pipe: bool,
    ):
        self._worker_pid = worker_pid
        self._launcher_process = launcher_process
        self._reader = reader
        self._writer = writer
        self._socket_dir = socket_dir
        self._socket_path = socket_path
        self._stdin = _TerminalStdin(self) if stdin_pipe else None
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._transport_error: str | None = None
        self._status_received = asyncio.Event()
        self._cleaned = False
        self._released = False
        self._control_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._wait_task: asyncio.Task[int] | None = None
        self._dispatch_task = asyncio.create_task(self._dispatch())
        _TERMINAL_ACTIVE_RUNS.add(self)

    @property
    def pid(self) -> int:
        return self._worker_pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @property
    def stdin(self):
        return self._stdin

    @property
    def stdout(self) -> asyncio.StreamReader:
        return self._stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        return self._stderr

    async def _dispatch(self) -> None:
        try:
            while True:
                kind, payload = await _read_terminal_frame(self._reader)
                if kind == _TERMINAL_STDOUT:
                    self.stdout.feed_data(payload)
                elif kind == _TERMINAL_STDERR:
                    self.stderr.feed_data(payload)
                elif kind == _TERMINAL_STATUS:
                    if len(payload) != 4:
                        raise ValueError("invalid terminal status frame")
                    self._returncode = struct.unpack("!i", payload)[0]
                    self._status_received.set()
                    # The worker writes status only after both output streams have reached EOF.
                    # No further frames are valid, so stop waiting for the worker's socket close.
                    break
                elif kind == _TERMINAL_ERROR:
                    self.stderr.feed_data(payload)
                else:
                    raise ValueError(f"unknown terminal IPC frame kind: {kind!r}")
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError) as error:
            if self._returncode is None:
                self._transport_error = str(error) or "terminal worker connection closed"
                self.stderr.feed_data(
                    f"terminal worker connection failed: {self._transport_error}\n".encode()
                )
                self._returncode = 255
                self._status_received.set()
        finally:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._status_received.set()

    def _write_control_nowait(self, kind: bytes, payload: bytes = b"") -> None:
        if self._cleaned or self._writer.is_closing():
            raise RuntimeError("terminal worker connection is closed")
        if len(kind) != 1 or len(payload) + 1 > _TERMINAL_MAX_FRAME:
            raise ValueError("invalid terminal IPC control frame")
        frame = _TERMINAL_FRAME_HEADER.pack(len(payload) + 1) + kind + payload
        self._writer.write(frame)

    async def _drain_control(self) -> None:
        if not self._writer.is_closing():
            await self._writer.drain()

    async def _send_control(self, kind: bytes, payload: bytes = b"") -> None:
        async with self._control_lock:
            if self._cleaned or self._writer.is_closing():
                raise RuntimeError("terminal worker connection is closed")
            await _write_terminal_frame(self._writer, kind, payload)

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if input is not None:
            if self._stdin is None:
                raise ValueError("input requires stdin=asyncio.subprocess.PIPE")
            self._stdin.write(input)
        if self._stdin is not None:
            self._stdin.close()
            await self._stdin.drain()
        stdout, stderr = await asyncio.gather(
            self.stdout.read(),
            self.stderr.read(),
        )
        await self.wait()
        return stdout, stderr

    async def wait(self) -> int:
        if self._wait_task is None:
            self._wait_task = asyncio.create_task(self._wait_for_completion())
        # A caller cancelling one wait must not cancel the shared lifecycle cleanup.
        return await asyncio.shield(self._wait_task)

    async def _wait_for_completion(self) -> int:
        await self._status_received.wait()
        await self._dispatch_task
        await self._shutdown_launcher()
        await self._cleanup()
        return self._returncode if self._returncode is not None else 255

    async def cancel(self) -> None:
        if self._cleaned:
            return
        if self._returncode is None:
            with contextlib.suppress((ConnectionError, OSError, RuntimeError)):
                await self._send_control(_TERMINAL_CANCEL)
        await self.wait()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            asyncio.get_running_loop().create_task(self._release_async())
        except RuntimeError:
            self._cleanup_at_exit()

    async def _release_async(self) -> None:
        if self._returncode is None and not self._cleaned:
            with contextlib.suppress((ConnectionError, OSError, RuntimeError)):
                await self._send_control(_TERMINAL_CANCEL)
        # ``release`` is synchronous for compatibility with RunHandle, but still waits for
        # the worker to observe cancellation while an event loop is available. A bounded
        # fallback prevents a broken launcher from keeping the socket directory forever.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.wait(), timeout=5.0)
        if not self._cleaned:
            await self._shutdown_launcher()
            await self._cleanup()

    async def _shutdown_launcher(self) -> None:
        process = self._launcher_process
        wait = getattr(process, "wait", None)
        if wait is None:
            return
        try:
            await asyncio.wait_for(wait(), timeout=0.5)
            return
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001, S110 - external launcher wait is best effort
            # A broken wait implementation must not prevent an exact launcher shutdown
            # attempt; the worker status has already established the target's result.
            pass

        if getattr(process, "returncode", None) is None:
            with contextlib.suppress(Exception):
                process.terminate()
            try:
                await asyncio.wait_for(wait(), timeout=0.5)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    process.kill()
                with contextlib.suppress(Exception):
                    await wait()
            except Exception:  # noqa: BLE001, S110 - external launcher wait is best effort
                pass

    async def _cleanup(self) -> None:
        async with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            with contextlib.suppress(Exception):
                self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            with contextlib.suppress(OSError):
                os.unlink(self._socket_path)
            with contextlib.suppress(OSError):
                os.rmdir(self._socket_dir)
            _TERMINAL_ACTIVE_RUNS.discard(self)

    def _cleanup_at_exit(self) -> None:
        """Close exact owned resources synchronously during interpreter shutdown."""
        if self._cleaned:
            return
        self._cleaned = True
        with contextlib.suppress(Exception):
            self._writer.close()
        process = self._launcher_process
        if getattr(process, "returncode", None) is None:
            with contextlib.suppress(Exception):
                process.terminate()
        with contextlib.suppress(OSError):
            os.unlink(self._socket_path)
        with contextlib.suppress(OSError):
            os.rmdir(self._socket_dir)
        _TERMINAL_ACTIVE_RUNS.discard(self)

    def __del__(self):
        with contextlib.suppress(Exception):
            self._writer.close()


def _cleanup_terminal_runs() -> None:
    for run in tuple(_TERMINAL_ACTIVE_RUNS):
        with contextlib.suppress(Exception):
            run._cleanup_at_exit()


atexit.register(_cleanup_terminal_runs)


class TerminalWindowExecutionHost:
    """Run a headless agent CLI inside a new visible terminal window.

    .. warning::
       This execution host is experimental. Its launcher and lifecycle contract may change in a
       later minor release.

    v1 is Linux-focused and supports only ``NoIsolation``.  The launcher is injected so
    terminal services remain an external, replaceable boundary; the default discovery is
    merely a convenience for local desktop use.
    """

    def __init__(
        self,
        launcher: TerminalWindowLauncher | None = None,
        *,
        startup_timeout: float = 10.0,
    ):
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be greater than zero")
        self.launcher = launcher
        self.startup_timeout = startup_timeout

    async def launch(
        self,
        command: list[str],
        cwd: str,
        *,
        env: dict[str, str] | None = None,
        stdin: int = asyncio.subprocess.DEVNULL,
        isolation_policy: IsolationPolicy | None = None,
    ) -> TerminalWindowRunHandle:
        if not sys.platform.startswith("linux"):
            raise TerminalWindowUnavailableError(
                "TerminalWindowExecutionHost v1 is only available on Linux"
            )
        if not isinstance(command, list) or not command:
            raise ValueError("command must not be empty")
        if not all(isinstance(value, str) for value in command):
            raise ValueError("command must contain only strings")
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a string")  # noqa: TRY004 - API validation contract
        if stdin not in (asyncio.subprocess.DEVNULL, asyncio.subprocess.PIPE):
            raise ValueError(
                "TerminalWindowExecutionHost supports only DEVNULL or PIPE stdin"
            )
        if env is not None and (
            not isinstance(env, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            )
        ):
            raise ValueError("env must be a mapping of string keys and values")
        policy = isolation_policy if isolation_policy is not None else NoIsolation()
        if type(policy) is not NoIsolation:
            raise IsolationUnavailableError(
                "TerminalWindowExecutionHost v1 supports only NoIsolation"
            )

        launcher = self.launcher if self.launcher is not None else discover_terminal_launcher()
        display_kind = getattr(launcher, "display", "any")
        if getattr(launcher, "requires_graphical", False) and not (
            _terminal_graphical_session_available(display_kind)
        ):
            raise TerminalWindowUnavailableError(
                "no usable graphical session is available for the terminal launcher"
            )

        socket_dir = tempfile.mkdtemp(prefix="agentshell-")
        os.chmod(socket_dir, 0o700)
        socket_path = os.path.join(socket_dir, "run.sock")
        if len(os.fsencode(socket_path)) >= 100:
            os.rmdir(socket_dir)
            raise TerminalWindowUnavailableError(
                "private IPC socket path is too long for this platform"
            )

        loop = asyncio.get_running_loop()
        connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            loop.create_future()
        )

        async def accept_client(reader, writer):
            if connection.done():
                writer.close()
                await writer.wait_closed()
                return
            connection.set_result((reader, writer))

        server = None
        launcher_process = None
        reader = writer = None
        launcher_wait_task = None
        try:
            try:
                server = await asyncio.start_unix_server(accept_client, path=socket_path)
                os.chmod(socket_path, 0o600)
            except OSError as error:
                raise TerminalWindowUnavailableError(
                    f"could not create private terminal IPC socket: {error}"
                ) from error

            launcher_env = _terminal_launcher_environment()
            # ``None`` means inherit the caller's environment for the target command, but it
            # must not make that environment part of the terminal launcher's argv/environment.
            run_env = dict(os.environ) if env is None else dict(env)
            worker_command = [
                sys.executable,
                "-m",
                "agent_shell.terminal_worker",
                "--socket",
                socket_path,
            ]
            try:
                launcher_process = await launcher.launch(
                    worker_command,
                    cwd=cwd,
                    env=launcher_env,
                )
            except TerminalWindowUnavailableError:
                raise
            except Exception as error:
                raise TerminalWindowUnavailableError(
                    f"terminal launcher failed: {error}"
                ) from error
            if launcher_process is None or not hasattr(launcher_process, "wait"):
                raise TerminalWindowUnavailableError(
                    "terminal launcher did not return a process handle"
                )

            connection_task = connection
            launcher_wait_task = asyncio.create_task(launcher_process.wait())
            done, _ = await asyncio.wait(
                (connection_task,),
                timeout=self.startup_timeout,
            )
            if connection_task in done:
                reader, writer = connection_task.result()
            else:
                if launcher_wait_task.done():
                    try:
                        code = launcher_wait_task.result()
                    except asyncio.CancelledError:
                        code = "cancelled"
                    except Exception as error:
                        raise TerminalWindowUnavailableError(
                            f"terminal launcher failed before starting worker: {error}"
                        ) from error
                    raise TerminalWindowUnavailableError(
                        f"terminal launcher exited before starting worker (status {code})"
                    )
                raise TerminalWindowUnavailableError(
                    "terminal worker did not connect before startup timeout"
                )
            launcher_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await launcher_wait_task
            launcher_wait_task = None

            try:
                kind, payload = await asyncio.wait_for(
                    _read_terminal_frame(reader),
                    timeout=self.startup_timeout,
                )
            except (TimeoutError, asyncio.IncompleteReadError, ValueError) as error:
                raise TerminalWindowUnavailableError(
                    f"terminal worker did not complete its startup handshake: {error}"
                ) from error
            if kind != _TERMINAL_HELLO:
                raise TerminalWindowUnavailableError(
                    "terminal worker sent an invalid startup message"
                )
            try:
                hello = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TerminalWindowUnavailableError(
                    "terminal worker sent malformed startup metadata"
                ) from error
            worker_pid = hello.get("pid")
            if not isinstance(worker_pid, int) or worker_pid <= 0:
                raise TerminalWindowUnavailableError(
                    "terminal worker did not report a valid PID"
                )
            server.close()
            server = None
            request = {
                "command": command,
                "cwd": cwd,
                "env": run_env,
                "stdin": "pipe" if stdin == asyncio.subprocess.PIPE else "devnull",
            }
            await _write_terminal_frame(
                writer,
                _TERMINAL_REQUEST,
                json.dumps(request, separators=(",", ":")).encode("utf-8"),
            )
            run = TerminalWindowRunHandle(
                worker_pid=worker_pid,
                launcher_process=launcher_process,
                reader=reader,
                writer=writer,
                socket_dir=socket_dir,
                socket_path=socket_path,
                stdin_pipe=stdin == asyncio.subprocess.PIPE,
            )
            return run
        except BaseException:
            if launcher_wait_task is not None and not launcher_wait_task.done():
                launcher_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await launcher_wait_task
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server.wait_closed(), timeout=0.5)
            with contextlib.suppress(OSError):
                os.unlink(socket_path)
            with contextlib.suppress(OSError):
                os.rmdir(socket_dir)
            if launcher_process is not None:
                with contextlib.suppress(Exception):
                    if getattr(launcher_process, "returncode", None) is None:
                        launcher_process.terminate()
                        try:
                            await asyncio.wait_for(launcher_process.wait(), timeout=0.5)
                        except TimeoutError:
                            with contextlib.suppress(Exception):
                                launcher_process.kill()
                            with contextlib.suppress(Exception):
                                await launcher_process.wait()
            raise
