"""Execution hosts and per-run handles.

An execution host decides where a CLI process runs.  Isolation is a separate choice so
future hosts such as tmux or Herdr can compose with the same policies instead of growing
one class for every host/policy combination.
"""

import asyncio
import contextlib
import os
import shutil
import struct
import sys
from dataclasses import dataclass
from typing import Protocol

from agent_shell.process_cleanup import (
    create_grouped_process,
    kill_process_group,
    release_process_group,
    transfer_process_guardian,
)


class RunHandle(Protocol):
    """The observable lifecycle of one launched command."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdin(self): ...

    @property
    def stdout(self): ...

    @property
    def stderr(self): ...

    async def wait(self) -> int: ...

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]: ...

    async def cancel(self) -> None: ...

    def release(self) -> None: ...


class IsolationUnavailableError(RuntimeError):
    """Raised when explicitly requested isolation cannot be provided."""


@dataclass(slots=True)
class PreparedLaunch:
    """A policy-prepared command and any file descriptors its host must preserve."""

    command: list[str]
    env: dict[str, str] | None
    pass_fds: tuple[int, ...] = ()
    reported_status_fd: int | None = None
    close_after_spawn: tuple[int, ...] = ()

    def spawned(self) -> None:
        for fd in self.close_after_spawn:
            with contextlib.suppress(OSError):
                os.close(fd)

    def failed(self) -> None:
        self.spawned()
        if self.reported_status_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.reported_status_fd)


class IsolationPolicy(Protocol):
    """Prepare the command boundary an execution host will launch."""

    async def prepare(
        self,
        command: list[str],
        env: dict[str, str] | None,
    ) -> PreparedLaunch: ...


class NoIsolation:
    """Run directly on the selected host, preserving the historical behaviour."""

    async def prepare(
        self,
        command: list[str],
        env: dict[str, str] | None,
    ) -> PreparedLaunch:
        return PreparedLaunch(command=list(command), env=env)


_PID_NAMESPACE_INIT = r"""
import os
import struct
import sys

status_fd = int(os.environ.pop("AGENTSHELL_STATUS_FD"))
command = sys.argv[1:]
child_pid = os.fork()
if child_pid == 0:
    os.close(status_fd)
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as error:
        os.write(2, f"could not execute {command[0]}: {error}\n".encode())
        os._exit(127)

child_status = None
while child_status is None:
    try:
        waited_pid, status = os.waitpid(-1, 0)
    except ChildProcessError:
        break
    if waited_pid == child_pid:
        child_status = status

if child_status is None:
    returncode = 255
elif os.WIFEXITED(child_status):
    returncode = os.WEXITSTATUS(child_status)
elif os.WIFSIGNALED(child_status):
    returncode = -os.WTERMSIG(child_status)
else:
    returncode = 255

os.write(status_fd, struct.pack("!i", returncode))
os.close(status_fd)
os._exit(returncode if 0 <= returncode <= 255 else 128 + (-returncode))
"""


class LinuxPidNamespaceIsolation:
    """Hide AgentShell's ancestors inside a rootless Linux PID namespace.

    This is direct-signal isolation, not a general security sandbox. The namespace's PID 1
    is a tiny reaper and the requested CLI runs as PID 2 or later.
    """

    def __init__(self):
        self._available = False
        self._unshare_path: str | None = None

    async def _ensure_available(self) -> str:
        if self._available and self._unshare_path is not None:
            return self._unshare_path
        if not sys.platform.startswith("linux"):
            raise IsolationUnavailableError(
                "Linux PID namespace isolation is only available on Linux"
            )

        unshare_path = shutil.which("unshare")
        if unshare_path is None:
            raise IsolationUnavailableError(
                "Linux PID namespace isolation requires the `unshare` command"
            )

        try:
            probe = await asyncio.create_subprocess_exec(
                unshare_path,
                "--user",
                "--map-current-user",
                "--pid",
                "--fork",
                "--mount-proc",
                "true",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise IsolationUnavailableError(
                f"Linux PID namespace isolation could not start `unshare`: {error}"
            ) from error
        _, stderr = await probe.communicate()
        if probe.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise IsolationUnavailableError(
                f"Linux PID namespace isolation is unavailable{suffix}"
            )

        self._available = True
        self._unshare_path = unshare_path
        return unshare_path

    async def prepare(
        self,
        command: list[str],
        env: dict[str, str] | None,
    ) -> PreparedLaunch:
        unshare_path = await self._ensure_available()
        status_read_fd, status_write_fd = os.pipe()
        child_env = dict(os.environ if env is None else env)
        child_env["AGENTSHELL_STATUS_FD"] = str(status_write_fd)
        wrapped_command = [
            unshare_path,
            "--user",
            "--map-current-user",
            "--pid",
            "--fork",
            "--mount-proc",
            sys.executable,
            "-I",
            "-S",
            "-c",
            _PID_NAMESPACE_INIT,
            *command,
        ]
        return PreparedLaunch(
            command=wrapped_command,
            env=child_env,
            pass_fds=(status_write_fd,),
            reported_status_fd=status_read_fd,
            close_after_spawn=(status_write_fd,),
        )


class NativeRunHandle:
    """A local subprocess together with AgentShell's exact cleanup ownership."""

    def __init__(
            self,
            process: asyncio.subprocess.Process,
            reported_status_fd: int | None = None,
    ):
        self._process = process
        self._reported_status_fd = reported_status_fd
        self._reported_returncode: int | None = None

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        if self._reported_returncode is not None:
            return self._reported_returncode
        return self._process.returncode

    @property
    def stdin(self):
        return self._process.stdin

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def stderr(self):
        return self._process.stderr

    async def wait(self) -> int:
        outer_returncode = await self._process.wait()
        if self._reported_status_fd is not None:
            status = b""
            try:
                while len(status) < 4:
                    chunk = os.read(self._reported_status_fd, 4 - len(status))
                    if not chunk:
                        break
                    status += chunk
            finally:
                os.close(self._reported_status_fd)
                self._reported_status_fd = None
            if len(status) == 4:
                self._reported_returncode = struct.unpack("!i", status)[0]
            else:
                self._reported_returncode = outer_returncode
        return self.returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        result = await self._process.communicate(input)
        await self.wait()
        return result

    async def cancel(self) -> None:
        kill_process_group(self)
        await self.wait()

    def release(self) -> None:
        release_process_group(self)

    def __del__(self):
        if self._reported_status_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._reported_status_fd)


class ExecutionHost(Protocol):
    """Factory for individual command runs."""

    async def launch(
        self,
        command: list[str],
        cwd: str,
        *,
        env: dict[str, str] | None = None,
        stdin: int = asyncio.subprocess.DEVNULL,
        isolation_policy: IsolationPolicy | None = None,
    ) -> RunHandle: ...


class NativeExecutionHost:
    """Launch commands as ordinary local subprocesses."""

    async def launch(
        self,
        command: list[str],
        cwd: str,
        *,
        env: dict[str, str] | None = None,
        stdin: int = asyncio.subprocess.DEVNULL,
        isolation_policy: IsolationPolicy | None = None,
    ) -> NativeRunHandle:
        policy = isolation_policy if isolation_policy is not None else NoIsolation()
        prepared = await policy.prepare(command, env)
        try:
            process = await create_grouped_process(
                prepared.command,
                cwd=cwd,
                env=prepared.env,
                stdin=stdin,
                pass_fds=prepared.pass_fds,
            )
        except BaseException:
            prepared.failed()
            raise
        prepared.spawned()
        run_handle = NativeRunHandle(
            process,
            reported_status_fd=prepared.reported_status_fd,
        )
        transfer_process_guardian(process, run_handle)
        return run_handle


def __getattr__(name: str):
    """Lazily preserve the historical ``agent_shell.execution`` tmux imports."""
    if name in {"TmuxExecutionHost", "TmuxPlacement", "TmuxUnavailableError"}:
        from agent_shell.tmux import (
            TmuxExecutionHost,
            TmuxPlacement,
            TmuxUnavailableError,
        )

        return {
            "TmuxExecutionHost": TmuxExecutionHost,
            "TmuxPlacement": TmuxPlacement,
            "TmuxUnavailableError": TmuxUnavailableError,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
