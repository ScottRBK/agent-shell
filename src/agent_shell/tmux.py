"""Optional tmux execution host and its per-run lifecycle handles."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from typing import Literal

from agent_shell import tmux_protocol
from agent_shell.execution import (
    IsolationPolicy,
    IsolationUnavailableError,
    NoIsolation,
    RunHandle,
)

_TMUX_COMMAND_TIMEOUT = 5.0


class TmuxUnavailableError(RuntimeError):
    """Raised when the optional tmux execution host cannot start a managed run."""


@dataclass(frozen=True, slots=True)
class TmuxPlacement:
    """Describe where a :class:`TmuxExecutionHost` should create a run.

    Placement is deliberately separate from cleanup ownership.  A ``new-session`` placement
    creates a session that the resulting run owns, while a ``new-window`` placement borrows the
    named session and owns only the window created for that run.
    """

    _kind: Literal["new-session", "new-window", "current-window"]
    _session_name: str | None = None
    _focus: bool = False

    @classmethod
    def new_session(cls, name: str | None = None) -> TmuxPlacement:
        """Create a placement that owns one newly-created tmux session."""
        if name is not None:
            _validate_tmux_name(name, "session name")
        return cls(
            _kind="new-session",
            _session_name=name,
        )

    @classmethod
    def new_window(
        cls,
        session: str,
        focus: bool = False,
    ) -> TmuxPlacement:
        """Create a placement that borrows ``session`` and owns one new window."""
        _validate_tmux_name(session, "session name")
        if not isinstance(focus, bool):
            raise TypeError("focus must be a bool")
        return cls(_kind="new-window", _session_name=session, _focus=focus)

    @classmethod
    def current_session(cls, focus: bool = False) -> TmuxPlacement:
        """Create a new-window placement targeting the caller's current tmux session.

        The environment is checked when a host launches the placement.  This keeps placement
        construction side-effect free while still producing a clear error outside tmux.
        """
        if not isinstance(focus, bool):
            raise TypeError("focus must be a bool")
        return cls(_kind="current-window", _focus=focus)

    @property
    def kind(self) -> Literal["new-session", "new-window", "current-window"]:
        """The resource creation operation represented by this placement."""
        return self._kind

    @property
    def session(self) -> str | None:
        """The explicit session name, when this placement has one."""
        return self._session_name

    @property
    def focus(self) -> bool:
        """Whether a newly-created window should become the active window."""
        return self._focus


def _validate_tmux_name(value: str, description: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{description} must be a string")
    if not value or "\x00" in value or ":" in value or "." in value or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ValueError(
            f"{description} must be a non-empty session name without target separators"
        )


async def _tmux_receive_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    try:
        return await tmux_protocol.receive_frame(reader)
    except tmux_protocol.TmuxProtocolError as error:
        raise TmuxUnavailableError(str(error)) from error


async def _tmux_send_frame(
    writer: asyncio.StreamWriter,
    channel: int,
    payload: bytes,
) -> None:
    try:
        await tmux_protocol.send_frame(writer, channel, payload)
    except tmux_protocol.TmuxProtocolError as error:
        raise TmuxUnavailableError(str(error)) from error


_TMUX_ACTIVE_RUNS: set[object] = set()


class _TmuxRunHandle:
    """RunHandle backed by one private tmux bridge connection."""

    def __init__(
        self,
        *,
        tmux_path: str,
        resource_kind: Literal["session", "window"],
        session_name: str,
        window_id: str | None,
        run_directory: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pid: int,
        stdin_pipe: bool,
    ):
        self._tmux_path = tmux_path
        self._resource_kind = resource_kind
        self._session_name = session_name
        self._window_id = window_id
        self._run_directory = run_directory
        self._reader = reader
        self._writer = writer
        self._pid = pid
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_future = asyncio.get_running_loop().create_future()
        self._receiver_task = asyncio.create_task(self._receive())
        self._released = False
        self._stdin = _TmuxStdin(self) if stdin_pipe else None
        _TMUX_ACTIVE_RUNS.add(self)

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @property
    def stdin(self):
        return self._stdin

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    async def _receive(self) -> None:
        try:
            while True:
                channel, payload = await _tmux_receive_frame(self._reader)
                if channel == tmux_protocol.STDOUT:
                    self._stdout.feed_data(payload)
                elif channel == tmux_protocol.STDERR:
                    self._stderr.feed_data(payload)
                elif channel == tmux_protocol.EXIT:
                    if len(payload) != 4:
                        raise TmuxUnavailableError("tmux bridge returned an invalid exit status")
                    self._returncode = struct.unpack("!i", payload)[0]
                    if not self._wait_future.done():
                        self._wait_future.set_result(self._returncode)
                    self._stdout.feed_eof()
                    self._stderr.feed_eof()
                elif channel == tmux_protocol.ERROR:
                    raise TmuxUnavailableError(payload.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError):
            if not self._wait_future.done():
                self._wait_future.set_exception(
                    TmuxUnavailableError("tmux bridge disconnected before reporting status")
                )
            self._stdout.feed_eof()
            self._stderr.feed_eof()
        except BaseException as error:  # noqa: BLE001 - settle waiters on any transport failure
            if not self._wait_future.done():
                self._wait_future.set_exception(error)
            self._stdout.feed_eof()
            self._stderr.feed_eof()

    async def wait(self) -> int:
        return await self._wait_future

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._stdin is None:
            if input is not None:
                raise ValueError("cannot send input to a tmux run with DEVNULL stdin")
        else:
            if input is not None:
                self._stdin.write(input)
            self._stdin.close()
            await self._stdin.wait_closed()
        stdout, stderr = await asyncio.gather(self._stdout.read(), self._stderr.read())
        await self.wait()
        return stdout, stderr

    async def cancel(self) -> None:
        if self._released:
            return
        try:
            await _tmux_send_frame(self._writer, tmux_protocol.CANCEL, b"")
            await self.wait()
        finally:
            self._released = True
            with contextlib.suppress(Exception):
                self._writer.write(
                    tmux_protocol.FRAME_HEADER.pack(tmux_protocol.RELEASE, 0)
                )
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._cleanup_resource()
            shutil.rmtree(self._run_directory, ignore_errors=True)
            _TMUX_ACTIVE_RUNS.discard(self)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receiver_task

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with contextlib.suppress(Exception):
            self._writer.write(
                tmux_protocol.FRAME_HEADER.pack(tmux_protocol.RELEASE, 0)
            )
        self._writer.close()
        if not self._receiver_task.done():
            self._receiver_task.cancel()
        self._cleanup_resource()
        shutil.rmtree(self._run_directory, ignore_errors=True)
        _TMUX_ACTIVE_RUNS.discard(self)

    def __del__(self):
        if getattr(self, "_released", True):
            return
        with contextlib.suppress(Exception):
            self._released = True
            self._writer.close()
            self._cleanup_resource()
            shutil.rmtree(self._run_directory, ignore_errors=True)
            _TMUX_ACTIVE_RUNS.discard(self)

    def _cleanup_resource(self) -> None:
        if self._resource_kind == "session":
            _tmux_kill_session(self._tmux_path, self._session_name)
        elif self._window_id:
            _tmux_kill_window(self._tmux_path, self._window_id)


def _cleanup_tmux_resource(
    tmux_path: str,
    resource_kind: Literal["session", "window"],
    session_name: str,
    window_id: str | None,
) -> None:
    if resource_kind == "session":
        _tmux_kill_session(tmux_path, session_name)
    elif window_id:
        _tmux_kill_window(tmux_path, window_id)


class _TmuxStdin:
    """Small StreamWriter-shaped stdin endpoint for a bridged PIPE run."""

    def __init__(self, handle: _TmuxRunHandle):
        self._handle = handle
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ValueError("write to closed tmux stdin")
        if not isinstance(data, bytes):
            raise TypeError("tmux stdin expects bytes")
        if len(data) > tmux_protocol.MAX_FRAME:
            raise ValueError("tmux bridge frame is too large")
        self._handle._writer.write(
            tmux_protocol.FRAME_HEADER.pack(tmux_protocol.STDIN, len(data)) + data
        )

    async def drain(self) -> None:
        await self._handle._writer.drain()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._handle._writer.write(
            tmux_protocol.FRAME_HEADER.pack(tmux_protocol.CLOSE_STDIN, 0)
        )

    async def wait_closed(self) -> None:
        await self.drain()


def _tmux_kill_session(tmux_path: str, session_name: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [tmux_path, "-f", "/dev/null", "kill-session", "-t", session_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )


def _tmux_kill_window(tmux_path: str, window_id: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [tmux_path, "-f", "/dev/null", "kill-window", "-t", window_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )


async def _tmux_current_session(tmux_path: str) -> str:
    tmux_pane = os.environ.get("TMUX_PANE")
    if not os.environ.get("TMUX") or not tmux_pane:
        raise TmuxUnavailableError(
            "current tmux session placement requires running inside tmux"
        )

    process = await asyncio.create_subprocess_exec(
        tmux_path,
        "-f",
        "/dev/null",
        "display-message",
        "-p",
        "-t",
        tmux_pane,
        "#{session_name}",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_TMUX_COMMAND_TIMEOUT
        )
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise TmuxUnavailableError(
            "tmux did not report the current session"
        ) from error
    session_name = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode != 0 or not session_name:
        detail = stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise TmuxUnavailableError(
            f"tmux could not determine the current session{suffix}"
        )
    return session_name


class TmuxExecutionHost:
    """Run one command in an AgentShell-owned tmux session or window.

    .. warning::
       This execution host is experimental. Its placement and lifecycle contract may change in a
       later minor release.

    The first version intentionally supports only :class:`NoIsolation` and DEVNULL/PIPE stdin.  A
    private worker keeps the CLI's raw stdout/stderr off tmux's merged PTY stream while mirroring
    both streams into the visible pane.
    """

    def __init__(self, placement: TmuxPlacement | None = None):
        if placement is not None and not isinstance(placement, TmuxPlacement):
            raise TypeError("placement must be a TmuxPlacement")
        self.placement = placement

    async def launch(
        self,
        command: list[str],
        cwd: str,
        *,
        env: dict[str, str] | None = None,
        stdin: int = asyncio.subprocess.DEVNULL,
        isolation_policy: IsolationPolicy | None = None,
    ) -> RunHandle:
        policy = isolation_policy if isolation_policy is not None else NoIsolation()
        if not isinstance(policy, NoIsolation):
            raise IsolationUnavailableError(
                "TmuxExecutionHost supports only NoIsolation; the requested isolation policy "
                "cannot be transported through tmux"
            )
        if stdin not in (asyncio.subprocess.DEVNULL, asyncio.subprocess.PIPE):
            raise TmuxUnavailableError(
                "TmuxExecutionHost v1 supports only asyncio.subprocess.DEVNULL or PIPE stdin"
            )

        tmux_path = shutil.which("tmux")
        if tmux_path is None:
            raise TmuxUnavailableError(
                "TmuxExecutionHost requires the optional `tmux` executable"
            )

        prepared = await policy.prepare(command, env)
        placement = self.placement or TmuxPlacement.new_session()
        if placement.kind == "current-window":
            session_name = await _tmux_current_session(tmux_path)
        else:
            session_name = placement.session or f"agentshell-{uuid.uuid4().hex}"
        run_directory = tempfile.mkdtemp(prefix="agentshell-tmux-")
        socket_path = os.path.join(run_directory, "bridge.sock")
        window_id: str | None = None
        resource_kind: Literal["session", "window"] = (
            "session" if placement.kind == "new-session" else "window"
        )
        resource_label = "session" if resource_kind == "session" else "window"
        connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            asyncio.get_running_loop().create_future()
        )
        handed_off = False

        async def accept_connection(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            if connection.done():
                writer.close()
                await writer.wait_closed()
                return
            connection.set_result((reader, writer))

        server: asyncio.Server | None = None
        resource_created = False
        try:
            server = await asyncio.start_unix_server(accept_connection, path=socket_path)
            tmux_command = [tmux_path, "-f", "/dev/null"]
            if placement.kind == "new-session":
                tmux_command.extend(
                    [
                        "new-session",
                        "-d",
                        "-s",
                        session_name,
                        "-P",
                        "-F",
                        "#{pane_id}",
                    ]
                )
            else:
                tmux_command.extend(
                    [
                        "new-window",
                        *([] if placement.focus else ["-d"]),
                        "-t",
                        session_name,
                        "-P",
                        "-F",
                        "#{window_id}",
                    ]
                )
            tmux_command.extend(
                [
                    "--",
                    sys.executable,
                    "-m",
                    "agent_shell.tmux_bridge",
                    "--socket",
                    socket_path,
                ]
            )
            process = await asyncio.create_subprocess_exec(
                *tmux_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=_TMUX_COMMAND_TIMEOUT
                )
            except TimeoutError as error:
                process.kill()
                await process.wait()
                raise TmuxUnavailableError(
                    f"tmux did not respond while creating the AgentShell {resource_label}"
                ) from error
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                suffix = f": {detail}" if detail else ""
                raise TmuxUnavailableError(
                    f"tmux could not create the AgentShell {resource_label}{suffix}"
                )
            resource_created = True
            if placement.kind != "new-session":
                window_id = stdout.decode("utf-8", errors="replace").strip()
                if not window_id:
                    raise TmuxUnavailableError(
                        "tmux created a run window but did not report its window id"
                    )

            reader, writer = await asyncio.wait_for(connection, timeout=5.0)
            channel, payload = await asyncio.wait_for(
                _tmux_receive_frame(reader), timeout=5.0
            )
            if channel != tmux_protocol.HELLO:
                raise TmuxUnavailableError("tmux bridge sent an invalid handshake")
            pid = int(payload.decode("ascii"))
            config_env = dict(os.environ if prepared.env is None else prepared.env)
            config = json.dumps(
                {
                    "command": prepared.command,
                    "cwd": cwd,
                    "env": config_env,
                    "stdin": (
                        "pipe" if stdin == asyncio.subprocess.PIPE else "devnull"
                    ),
                },
                separators=(",", ":"),
            ).encode("utf-8")
            await _tmux_send_frame(writer, tmux_protocol.CONFIG, config)
            run_handle = _TmuxRunHandle(
                tmux_path=tmux_path,
                resource_kind=resource_kind,
                session_name=session_name,
                window_id=window_id,
                run_directory=run_directory,
                reader=reader,
                writer=writer,
                pid=pid,
                stdin_pipe=stdin == asyncio.subprocess.PIPE,
            )
            handed_off = True
            return run_handle
        except FileNotFoundError as error:
            raise TmuxUnavailableError(
                "TmuxExecutionHost could not execute the `tmux` binary"
            ) from error
        except (TimeoutError, OSError) as error:
            raise TmuxUnavailableError(
                f"TmuxExecutionHost could not start a run: {error}"
            ) from error
        finally:
            # `wait_closed()` may wait for an accepted Unix-socket connection as well as the
            # listening socket on some Python event-loop implementations.  A successful run
            # deliberately keeps that connection open until its RunHandle is released, so close
            # only the listener here; the run handle owns the accepted transport from this point.
            if server is not None:
                server.close()
            if not handed_off:
                if resource_created:
                    _cleanup_tmux_resource(
                        tmux_path, resource_kind, session_name, window_id
                    )
                shutil.rmtree(run_directory, ignore_errors=True)


def _cleanup_tmux_runs() -> None:
    for run in list(_TMUX_ACTIVE_RUNS):
        with contextlib.suppress(Exception):
            run.release()


atexit.register(_cleanup_tmux_runs)

__all__ = ["TmuxExecutionHost", "TmuxPlacement", "TmuxUnavailableError"]
