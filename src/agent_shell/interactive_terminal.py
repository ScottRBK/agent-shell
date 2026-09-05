"""Shared experimental control of a real harness terminal, with no screen interpretation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from agent_shell.execution import IsolationPolicy, IsolationUnavailableError, NoIsolation
from agent_shell.tmux import TmuxPlacement, TmuxUnavailableError, _tmux_current_session


class TmuxTerminalSession:
    """Own one interactive pane and its lifetime. Use ``close()`` or ``async with``.

    The UI runs directly on tmux's PTY. Screen capture is for inspection, never a success
    signal. The terminal is retained after child exit until explicitly closed.
    """

    def __init__(self, tmux: str, directory: Path):
        self._tmux = tmux
        self._directory = directory
        self._environment = os.environ.copy()
        self._input_lock = asyncio.Lock()
        self._resource: tuple[str, str] | None = None
        self.pane_id = ""
        self.window_id = ""
        self.session_name = ""
        self.pid = 0
        self.returncode: int | None = None
        self.closed = False
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
        if placement.kind == "current-window":
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
            else:
                args = ["new-window", "-t", session]
                if not placement.focus:
                    args.append("-d")
            args += ["-P", "-F", "#{pane_id} #{window_id}", "--",
                     sys.executable, worker, str(directory)]
            identity = (await terminal._command(*args)).strip().split()
            if len(identity) != 2 or not identity[0].startswith("%"):
                raise TmuxUnavailableError("tmux did not return an interactive pane ID")
            terminal.pane_id, terminal.window_id = identity
            terminal._resource = (
                ("kill-session", session) if placement.kind == "new-session"
                else ("kill-window", terminal.window_id)
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

    async def _command(self, *args: str, data: bytes | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            self._tmux, "-f", "/dev/null", *args, env=self._environment,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), 5)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise TmuxUnavailableError(stderr.decode(errors="replace").strip())
        return stdout.decode(errors="replace")

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError("Interactive terminal is closed")

    def _status(self) -> dict:
        try:
            return json.loads((self._directory / "status.json").read_text())
        except FileNotFoundError:
            return {}

    async def capture_screen(self) -> str:
        """Capture the current screen and retained scrollback for human inspection."""
        self._check_open()
        return await self._command("capture-pane", "-p", "-J", "-S", "-", "-t", self.pane_id)

    async def send_text(self, text: str, *, submit: bool = False) -> None:
        """Paste literal text. The caller must ensure the harness is ready for input."""
        self._check_open()
        if any(ord(char) < 32 and char not in "\n\t" for char in text) or "\x7f" in text:
            raise ValueError("Text must not contain terminal control characters")
        async with self._input_lock:
            buffer = f"agentshell-{uuid.uuid4().hex}"
            try:
                await self._command("load-buffer", "-b", buffer, "-", data=text.encode())
                await self._command("paste-buffer", "-d", "-p", "-b", buffer, "-t", self.pane_id)
                if submit:
                    await self._command("send-keys", "-t", self.pane_id, "Enter")
            finally:
                with contextlib.suppress(TmuxUnavailableError):
                    await self._command("delete-buffer", "-b", buffer)

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

    async def send_key(self, key: str) -> None:
        """Send one supported control key, for menus, submission, or interruption."""
        self._check_open()
        if key not in {"Enter", "Escape", "C-c", "C-d", "Tab", "Up", "Down", "Left", "Right"}:
            raise ValueError(f"Unsupported terminal key: {key}")
        async with self._input_lock:
            await self._command("send-keys", "-t", self.pane_id, key)

    async def resize(self, *, columns: int, rows: int) -> None:
        self._check_open()
        if type(columns) is not int or type(rows) is not int or min(columns, rows) < 2:
            raise ValueError("Terminal dimensions must be integers of at least 2")
        await self._command("resize-window", "-t", self.window_id,
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
            if self.pid and self.returncode is None and not self._lost:
                (self._directory / "stop").touch()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(2):
                        while self.returncode is None:
                            self.returncode = self._status().get("returncode")
                            await asyncio.sleep(0.02)
            if self._resource:
                operation, target = self._resource
                with contextlib.suppress(TmuxUnavailableError, TimeoutError, OSError):
                    await self._command(operation, "-t", target)
        finally:
            # Releasing the FIFO lets the worker clean up even when tmux control has failed.
            os.close(self._owner_fd)
            self.closed = True
            shutil.rmtree(self._directory, ignore_errors=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
