"""Execution contracts and launch-resource ownership, without host implementations."""

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Protocol


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
