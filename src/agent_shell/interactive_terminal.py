"""Shared experimental control of a real harness terminal, with no screen interpretation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import uuid

from agent_shell.execution import IsolationPolicy, IsolationUnavailableError, NoIsolation
from agent_shell.tmux_ownership import IDENTITY_FORMAT, TmuxResource
from agent_shell.tmux import (
    TmuxPlacement,
    TmuxTarget,
    TmuxUnavailableError,
    _run_tmux_command,
    _tmux_current_session,
    _tmux_exact_session_target,
)


class TmuxTerminalConnection:
    """Borrow input/screen transport without worker files or destructive cleanup."""

    def __init__(self, tmux: str, *, socket_path: str | None = None):
        self._tmux = tmux
        self._socket_path = socket_path
        self._environment = os.environ.copy()
        self._input_lock = asyncio.Lock()
        self._target: TmuxTarget | None = None
        self.pane_id = ""
        self.closed = False

    @property
    def target(self) -> TmuxTarget:
        """The captured address suitable for passing to another caller."""
        if self._target is None:
            raise RuntimeError("Terminal has no captured target")
        return self._target

    @classmethod
    async def attach(cls, target: object) -> TmuxTerminalConnection:
        if not isinstance(target, TmuxTarget):
            raise TypeError("TmuxExecutionHost attachment requires a TmuxTarget")
        tmux = shutil.which("tmux")
        if tmux is None:
            raise TmuxUnavailableError("Attaching requires the optional `tmux` executable")
        connection = cls(tmux, socket_path=target.socket_path)
        connection._target = target
        connection.pane_id = target.pane_id
        await connection._pane_command("display-message", "-p", "-t", target.pane_id, "#{pane_id}")
        return connection

    async def _command(self, *args: str, data: bytes | None = None) -> str:
        return await _run_tmux_command(
            self._tmux, *args, socket_path=self._socket_path, env=self._environment, data=data,
        )

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError("Interactive terminal is closed")

    async def _pane_command(self, *args: str) -> str:
        self._check_open()
        target = self.target
        checks = [
            f"#{{==:#{{pid}},{target.server_pid}}}",
            f"#{{==:#{{start_time}},{target.server_started}}}",
            f"#{{==:#{{pane_pid}},{target.pane_pid}}}",
            "#{==:#{pane_dead},0}",
        ]
        condition = checks[0]
        for check in checks[1:]:
            condition = f"#{{&&:{condition},{check}}}"
        # Keep the check and operation on the server's command queue, not as separate probes.
        # Only validated IDs and generated buffer names enter this command, never message text.
        marker = f"agentshell-ok-{uuid.uuid4().hex}"
        command = shlex.join(args) + " ; display-message -p " + marker
        output = await self._command(
            "if-shell", "-F", "-t", target.pane_id, condition, command,
        )
        if not output.endswith(marker + "\n"):
            raise TmuxUnavailableError("The captured tmux target is unavailable or has changed")
        return output[:-len(marker) - 1]

    async def capture_screen(self) -> str:
        """Capture the current screen and retained scrollback for human inspection."""
        self._check_open()
        return await self._pane_command("capture-pane", "-p", "-J", "-S", "-", "-t", self.pane_id)

    async def send_text(self, text: str, *, submit: bool = False) -> None:
        """Paste literal text. The caller must ensure the harness is ready for input."""
        self._check_open()
        if any(ord(char) < 32 and char not in "\n\t" for char in text) or "\x7f" in text:
            raise ValueError("Text must not contain terminal control characters")
        async with self._input_lock:
            self._check_open()
            buffer = f"agentshell-{uuid.uuid4().hex}"
            try:
                await self._command("load-buffer", "-b", buffer, "-", data=text.encode())
                await self._pane_command(
                    "paste-buffer", "-d", "-p", "-b", buffer, "-t", self.pane_id,
                )
                if submit:
                    await self._pane_command("send-keys", "-t", self.pane_id, "Enter")
            finally:
                with contextlib.suppress(TmuxUnavailableError):
                    await self._command("delete-buffer", "-b", buffer)

    async def send_key(self, key: str) -> None:
        """Send one supported control key, for menus, submission, or interruption."""
        self._check_open()
        if key not in {"Enter", "Escape", "C-c", "C-d", "Tab", "Up", "Down", "Left", "Right"}:
            raise ValueError(f"Unsupported terminal key: {key}")
        async with self._input_lock:
            self._check_open()
            await self._pane_command("send-keys", "-t", self.pane_id, key)

    async def close(self) -> None:
        """Disconnect locally. Never signal, resize, or remove the external terminal."""
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class TmuxTerminalSession(TmuxTerminalConnection):
    """Own one interactive pane and its lifetime. Use ``close()`` or ``async with``.

    The UI runs directly on tmux's PTY. Screen capture is for inspection, never a success
    signal. The terminal is retained after child exit until explicitly closed.
    """

    def __init__(self, tmux: str, directory: Path):
        super().__init__(tmux)
        self._directory = directory
        self._resource: TmuxResource | None = None
        self.window_id = ""
        self.session_name = ""
        self.pid = 0
        self.returncode: int | None = None
        self._close_task: asyncio.Task | None = None
        self._lost = False
        os.mkfifo(directory / "owner", mode=0o600)
        self._owner_fd = os.open(directory / "owner", os.O_RDWR | os.O_NONBLOCK)

    @classmethod
    async def launch(
        cls, command: list[str], cwd: str, *, placement: TmuxPlacement | None = None,
        env: dict[str, str] | None = None, isolation_policy: IsolationPolicy | None = None,
    ) -> TmuxTerminalSession:
        policy = isolation_policy if isolation_policy is not None else NoIsolation()
        if not isinstance(policy, NoIsolation):
            raise IsolationUnavailableError("Interactive tmux supports only NoIsolation")
        if not command or not Path(cwd).is_dir():
            raise ValueError("A command and an existing working directory are required")
        tmux = shutil.which("tmux")
        if tmux is None:
            raise TmuxUnavailableError(
                "Interactive sessions require the optional `tmux` executable"
            )
        placement = placement or TmuxPlacement.new_session()
        session = placement.session or f"agentshell-ui-{uuid.uuid4().hex}"
        if placement.kind in {"current-window", "split-pane"}:
            session = await _tmux_current_session(tmux)
        directory = Path(tempfile.mkdtemp(prefix="agentshell-ui-"))
        terminal = cls(tmux, directory)
        terminal.session_name = session
        try:
            (directory / "launch.json").write_text(json.dumps({
                "command": command, "cwd": str(Path(cwd).resolve()),
                "env": dict(env) if env is not None else os.environ.copy(),
            }))
            worker = str(Path(__file__).with_name("interactive_worker.py"))
            if placement.kind == "new-session":
                args = ["new-session", "-d", "-s", session, "-x", "100", "-y", "30"]
            elif placement.kind == "split-pane":
                args = ["split-window", "-h" if placement.direction == "right" else "-v",
                        "-t", os.environ["TMUX_PANE"]]
                if not placement.focus:
                    args.append("-d")
            else:
                args = ["new-window", "-t", _tmux_exact_session_target(session)]
                if not placement.focus:
                    args.append("-d")
            args += ["-P", "-F", IDENTITY_FORMAT, "--",
                     sys.executable, worker, str(directory)]
            identity = await terminal._command(*args)
            kind = ("session" if placement.kind == "new-session"
                    else "pane" if placement.kind == "split-pane" else "window")
            try:
                terminal._resource = TmuxResource.from_identity(kind, identity, directory)
            except ValueError as error:
                raise TmuxUnavailableError(str(error)) from error
            terminal.pane_id = terminal._resource.pane_id
            terminal.window_id = terminal._resource.window_id
            terminal._socket_path = terminal._resource.socket_path
            terminal._target = await TmuxTarget.capture(
                terminal.pane_id, socket_path=terminal._socket_path,
            )
            (directory / "start").touch()
            async with asyncio.timeout(5):
                while not (status := terminal._status()):
                    await asyncio.sleep(0.02)
            if "error" in status:
                raise TmuxUnavailableError(status["error"])
            terminal.pid = status["pid"]
            terminal.returncode = status["returncode"]
            return terminal
        except BaseException:
            await terminal.close()
            raise

    def _status(self) -> dict:
        try:
            return json.loads((self._directory / "status.json").read_text())
        except FileNotFoundError:
            return {}

    async def wait(self) -> int:
        """Wait for process exit, which is distinct from a completed agent turn."""
        last_probe = 0.0
        while self.returncode is None:
            self._check_open()
            self.returncode = self._status().get("returncode")
            if self.returncode is None:
                now = asyncio.get_running_loop().time()
                if now - last_probe >= 0.5:
                    try:
                        await self._command("display-message", "-p", "-t", self.pane_id,
                                            "#{pane_id}")
                    except TmuxUnavailableError as error:
                        self._lost = True
                        raise RuntimeError(
                            "Interactive terminal disappeared before exit status was recorded"
                        ) from error
                    last_probe = now
                await asyncio.sleep(0.05)
        return self.returncode

    async def resize(self, *, columns: int, rows: int) -> None:
        self._check_open()
        if type(columns) is not int or type(rows) is not int or min(columns, rows) < 2:
            raise ValueError("Terminal dimensions must be integers of at least 2")
        split = self._resource is not None and self._resource.kind == "pane"
        await self._command("resize-pane" if split else "resize-window",
                            "-t", self.pane_id if split else self.window_id,
                            "-x", str(columns), "-y", str(rows))

    async def close(self) -> None:
        """Close once, including when concurrent callers close or one caller is cancelled."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self.closed:
            return
        try:
            self.returncode = self._status().get("returncode", self.returncode)
            if self.pid and not self._lost and self._directory.exists():
                (self._directory / "stop").touch()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(2):
                        while True:
                            status = self._status()
                            self.returncode = status.get("returncode", self.returncode)
                            if status.get("stopped"):
                                break
                            await asyncio.sleep(0.02)
            if self._resource:
                with contextlib.suppress(TmuxUnavailableError, TimeoutError, OSError):
                    await self._command(*self._resource.cleanup_args())
        finally:
            # Releasing the FIFO lets the worker clean up even when tmux control has failed.
            os.close(self._owner_fd)
            self.closed = True
            shutil.rmtree(self._directory, ignore_errors=True)
