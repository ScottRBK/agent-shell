"""Experimental interactive session contracts and shared event delivery.

Terminal transport is owned by the host. All harness-specific commands and event parsing
are supplied by the adapter. No terminal screen is parsed into structured results.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from collections.abc import AsyncIterator, Callable
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from agent_shell.ports.execution import IsolationPolicy
from agent_shell.ports.interactive import (
    InteractiveAdapter as InteractiveAdapter,
    InteractiveAttachmentHost as InteractiveAttachmentHost,
    InteractiveExecutionHost as InteractiveExecutionHost,
    InteractiveLaunch as InteractiveLaunch,
    InteractiveTerminal as InteractiveTerminal,
    TerminalControl as TerminalControl,
)
from agent_shell.models.agent import StreamEvent
from agent_shell.models.resources import SessionResources
from agent_shell.resources import snapshot_resources


class InteractiveConnection:
    """Borrow terminal controls. Closing disconnects; it never stops the external agent.

    Unlike a launched InteractiveSession, this connection has no structured event stream,
    process wait, or exit status. Terminal input is not an agent-level delivery acknowledgement.
    """

    def __init__(self, terminal: TerminalControl):
        self.terminal = terminal

    async def close(self) -> None:
        await self.terminal.close()

    async def __aenter__(self) -> InteractiveConnection:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def event_writer_command(directory: Path) -> list[str]:
    """Command accepting a JSON object on stdin or as its final argument."""
    return [sys.executable, str(Path(__file__).with_name("interactive_event_writer.py")),
            str(directory / "events.jsonl")]


class InteractiveSession:
    def __init__(self, terminal: InteractiveTerminal, directory: Path, launch: InteractiveLaunch):
        self.terminal = terminal
        self.capabilities = launch.capabilities
        self._directory = directory
        self._parse_event = launch.parse_event
        self._reading = False
        self._offset = launch.event_offset
        self._event_path = launch.event_path or (lambda: directory / "events.jsonl")
        self._pending: deque[StreamEvent] = deque()
        self._ended = False

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Read hook/extension events until close. Only one reader is allowed at a time.

        Events describe the interactive conversation, including manually submitted turns.
        ``result`` is emitted only by an adapter with a positive completion signal.
        Use capabilities to distinguish unavailable metrics from actual reported zero values.
        """
        if self._reading:
            raise RuntimeError("An interactive session permits only one event reader")
        if self._ended or self.terminal.closed:
            return
        self._reading = True
        exit_task = asyncio.create_task(self.terminal.wait())
        exit_seen: float | None = None
        try:
            with contextlib.ExitStack() as files:
                log = None
                while not self.terminal.closed:
                    if self._pending:
                        yield self._pending.popleft()
                        continue
                    if log is None:
                        path = self._event_path()
                        if path is not None:
                            with contextlib.suppress(FileNotFoundError):
                                log = files.enter_context(path.open("rb"))
                                log.seek(self._offset)
                    start = log.tell() if log is not None else 0
                    line = log.readline(8 * 1024 * 1024 + 1) if log is not None else b""
                    if line.endswith(b"\n"):
                        self._offset = log.tell()
                        try:
                            record = json.loads(line)
                            if not isinstance(record, dict):
                                raise ValueError("Expected an event object")
                            events = self._parse_event(record)
                        except (ValueError, TypeError, KeyError) as error:
                            yield StreamEvent(
                                type="error", content=f"Invalid interactive event: {error}",
                            )
                        else:
                            self._pending.extend(events)
                        continue
                    if len(line) > 8 * 1024 * 1024:
                        raise ValueError("Interactive event exceeds 8 MiB")
                    if log is not None:
                        log.seek(start)
                    if exit_task.done():
                        # Notification subprocesses can outlive the harness briefly. This grace
                        # only drains records; it never infers a completed turn from silence.
                        now = asyncio.get_running_loop().time()
                        if exit_seen is None:
                            exit_seen = now
                        elif now - exit_seen >= 0.25:
                            self._ended = True
                            if line:
                                yield StreamEvent(
                                    type="error", content="Incomplete interactive event",
                                )
                            try:
                                status = exit_task.result()
                            except Exception as error:
                                yield StreamEvent(type="error", content=str(error))
                            else:
                                yield StreamEvent(
                                    type="process_exit", content="Harness process exited",
                                    returncode=status, signal=-status if status < 0 else None,
                                )
                            return
                    await asyncio.sleep(0.05)
        finally:
            self._reading = False
            exit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await exit_task

    async def close(self) -> None:
        await self.terminal.close()
        shutil.rmtree(self._directory, ignore_errors=True)

    async def __aenter__(self) -> InteractiveSession:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def open_interactive_session(
    adapter: InteractiveAdapter, host: InteractiveExecutionHost, policy: IsolationPolicy,
    cwd: str, *, prompt: str | None, model: str | None, effort: str | None,
    session_id: str | None, allowed_tools: list[str] | None = None,
    resources: SessionResources | None = None,
    on_session: Callable[[str | None], None] | None = None,
) -> InteractiveSession:
    adapter.validate_interactive(allowed_tools=allowed_tools)
    directory = Path(tempfile.mkdtemp(prefix="agentshell-events-"))
    try:
        (directory / "events.jsonl").touch(mode=0o600)
        resource_launch = None
        if resources is not None:
            resource_launch = await adapter.prepare_resources(
                snapshot_resources(resources, cwd), cwd, directory,
            )
        launch = adapter.prepare_interactive(
            directory, prompt=prompt, model=model, effort=effort,
            session_id=session_id, allowed_tools=allowed_tools,
            **({"resource_launch": resource_launch} if resource_launch is not None else {}),
        )
        if on_session is not None:
            parser = launch.parse_event

            def parse_with_selection(record: dict) -> list[StreamEvent]:
                events = parser(record)
                for event in events:
                    on_session(event.session_id)
                return events

            launch.parse_event = parse_with_selection
        terminal = await host.launch_interactive(
            launch.command, cwd, env={**os.environ, **(launch.env or {})}, isolation_policy=policy,
        )
        return InteractiveSession(terminal, directory, launch)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
