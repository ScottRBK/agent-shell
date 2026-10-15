"""Terminal and interactive-agent contracts, without transport implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_shell.models.agent import StreamEvent
from agent_shell.ports.execution import IsolationPolicy
from agent_shell.ports.resources import ResourceAdapter, ResourceLaunch


class TerminalControl(Protocol):
    """Input and inspection, without claiming ownership or process-exit information."""

    closed: bool

    async def capture_screen(self) -> str: ...
    async def send_text(self, text: str, *, submit: bool = False) -> None: ...
    async def send_key(self, key: str) -> None: ...
    async def close(self) -> None: ...


class InteractiveTerminal(TerminalControl, Protocol):
    returncode: int | None

    async def resize(self, *, columns: int, rows: int) -> None: ...
    async def wait(self) -> int: ...


@runtime_checkable
class InteractiveAttachmentHost(Protocol):
    """Optional capability, independent of launching interactive processes."""

    async def attach_interactive(self, target: object) -> TerminalControl: ...


@runtime_checkable
class InteractiveExecutionHost(Protocol):
    async def launch_interactive(
        self, command: list[str], cwd: str, *, env: dict[str, str] | None = None,
        isolation_policy: IsolationPolicy | None = None,
    ) -> InteractiveTerminal: ...


@dataclass
class InteractiveLaunch:
    command: list[str]
    parse_event: Callable[[dict], list[StreamEvent]]
    capabilities: frozenset[str]
    env: dict[str, str] | None = None
    event_path: Callable[[], Path | None] | None = None
    event_offset: int = 0


@runtime_checkable
class InteractiveAdapter(ResourceAdapter, Protocol):
    def validate_interactive(self, *, allowed_tools: list[str] | None) -> None:
        """Reject unsupported tool options before resource preparation has side effects."""
        ...

    def prepare_interactive(
        self, directory: Path, *, prompt: str | None, model: str | None,
        effort: str | None, session_id: str | None, allowed_tools: list[str] | None,
        resource_launch: ResourceLaunch | None = None,
    ) -> InteractiveLaunch: ...
