"""Herdr-backed execution host and its private one-shot bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_shell.execution import (
    IsolationPolicy,
    IsolationUnavailableError,
    NoIsolation,
    RunHandle,
)
from agent_shell.herdr_protocol import (
    BRIDGE_CONFIG,
    CANCEL,
    EXIT,
    HELLO,
    LAUNCH,
    LAUNCH_ERROR,
    LAUNCH_READY,
    STDERR,
    STDIN,
    STDIN_EOF,
    STDOUT,
    read_frame,
    write_frame,
)


class HerdrUnavailableError(RuntimeError):
    """Raised when the explicitly requested Herdr host cannot be used."""


@dataclass(slots=True)
class HerdrPane:
    """The owned Herdr resources returned by a client after creating a pane."""

    pane_id: str
    workspace_id: str | None = None


class HerdrClient(Protocol):
    """External Herdr control boundary used by :class:`HerdrExecutionHost`."""

    async def create_pane(
        self,
        *,
        cwd: str,
        argv: list[str],
        label: str,
    ) -> HerdrPane: ...

    async def close_pane(self, pane: HerdrPane) -> None: ...


async def _close_pane_with_timeout(
    client: HerdrClient,
    pane: HerdrPane,
    timeout: float,
) -> None:
    try:
        await asyncio.wait_for(client.close_pane(pane), timeout=timeout)
    except TimeoutError as error:
        raise HerdrUnavailableError(
            "Herdr cleanup timed out while closing Herdr resources"
        ) from error


class _CliHerdrClient:
    """Small stdlib client for the stable Herdr CLI JSON wrappers."""

    def __init__(
        self,
        herdr_command: str | Sequence[str] = "herdr",
        session: str | None = None,
        cleanup_timeout: float = 5.0,
    ) -> None:
        self._herdr_command = (
            [herdr_command]
            if isinstance(herdr_command, str)
            else list(herdr_command)
        )
        if not self._herdr_command:
            raise ValueError("herdr_command must not be empty")
        if cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be positive")
        self._session = session
        self._cleanup_timeout = cleanup_timeout

    def _argv(self, *args: str) -> list[str]:
        command = list(self._herdr_command)
        if self._session is not None:
            command.extend(["--session", self._session])
        command.extend(args)
        return command

    async def _run(self, *args: str) -> bytes:
        command = self._argv(*args)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise HerdrUnavailableError(
                f"Herdr executable {self._herdr_command[0]!r} is not installed"
            ) from error
        except OSError as error:
            raise HerdrUnavailableError(
                f"could not start Herdr executable {self._herdr_command[0]!r}: {error}"
            ) from error
        try:
            stdout, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            rendered = shlex.join(command)
            suffix = f": {detail}" if detail else ""
            raise HerdrUnavailableError(
                f"Herdr command {rendered} failed with exit code "
                f"{process.returncode}{suffix}"
            )
        return stdout

    async def _best_effort_close(self, *args: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._run(*args), timeout=self._cleanup_timeout
            )

    async def create_pane(
        self,
        *,
        cwd: str,
        argv: list[str],
        label: str,
    ) -> HerdrPane:
        output = await self._run(
            "workspace",
            "create",
            "--cwd",
            cwd,
            "--label",
            label,
            "--no-focus",
        )
        try:
            response = json.loads(output)
            result = response["result"]
            workspace_id = result["workspace"]["workspace_id"]
            pane_id = result["root_pane"]["pane_id"]
        except (KeyError, TypeError, ValueError) as error:
            raise HerdrUnavailableError(
                "Herdr workspace creation returned an invalid JSON response"
            ) from error

        try:
            await self._run("pane", "run", pane_id, shlex.join(argv))
        except BaseException:
            await self._best_effort_close("pane", "close", pane_id)
            await self._best_effort_close("workspace", "close", workspace_id)
            raise
        return HerdrPane(pane_id=pane_id, workspace_id=workspace_id)

    async def close_pane(self, pane: HerdrPane) -> None:
        await self._best_effort_close("pane", "close", pane.pane_id)
        if pane.workspace_id is not None:
            await self._best_effort_close("workspace", "close", pane.workspace_id)


class _IPCStdin:
    """StreamWriter-shaped stdin facade backed by framed bridge messages."""

    def __init__(self, run: _HerdrRunHandle) -> None:
        self._run = run
        self._pending: list[asyncio.Task[None]] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("Herdr run stdin is closed")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("Herdr run stdin accepts bytes-like values")
        task = asyncio.create_task(self._run._send(STDIN, bytes(data)))
        self._pending.append(task)

    async def drain(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, []
            await asyncio.gather(*pending)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pending.append(asyncio.create_task(self._run._send(STDIN_EOF)))

    def is_closing(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        await self.drain()


class _HerdrRunHandle:
    def __init__(
        self,
        *,
        pid: int,
        pane: HerdrPane,
        client: HerdrClient,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        stdin_pipe: bool,
        run_dir: str,
        cleanup_timeout: float,
    ) -> None:
        self._pid = pid
        self._pane = pane
        self._client = client
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        loop = asyncio.get_running_loop()
        self._wait_result = loop.create_future()
        self._launch_result = loop.create_future()
        self._reader_task = asyncio.create_task(self._consume_frames())
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_started = False
        self._run_dir = run_dir
        self._cleanup_timeout = cleanup_timeout
        self._stdin = _IPCStdin(self) if stdin_pipe else None

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

    async def _send(self, kind: bytes, payload: bytes = b"") -> None:
        async with self._write_lock:
            await write_frame(self._writer, kind, payload)

    async def _consume_frames(self) -> None:
        try:
            while True:
                kind, payload = await read_frame(self._reader)
                if kind == STDOUT:
                    self._stdout.feed_data(payload)
                elif kind == STDERR:
                    self._stderr.feed_data(payload)
                elif kind == EXIT:
                    response = json.loads(payload.decode("utf-8"))
                    self._returncode = int(response["returncode"])
                    if not self._wait_result.done():
                        self._wait_result.set_result(self._returncode)
                    break
                elif kind == LAUNCH_READY:
                    if not self._launch_result.done():
                        self._launch_result.set_result(None)
                elif kind == LAUNCH_ERROR:
                    error = self._launch_error(payload)
                    if not self._launch_result.done():
                        self._launch_result.set_exception(error)
                    if not self._wait_result.done():
                        self._wait_result.set_exception(error)
                    break
                else:
                    raise RuntimeError(
                        f"Herdr bridge received unexpected frame {kind!r}"
                    )
        except asyncio.CancelledError:
            raise
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
        ) as error:
            bridge_error = HerdrUnavailableError(
                f"Herdr bridge connection failed: {error}"
            )
            if not self._launch_result.done():
                self._launch_result.set_exception(bridge_error)
            if not self._wait_result.done():
                self._wait_result.set_exception(bridge_error)
        finally:
            self._stdout.feed_eof()
            self._stderr.feed_eof()

    @staticmethod
    def _launch_error(payload: bytes) -> OSError:
        try:
            response = json.loads(payload.decode("utf-8"))
            message = str(response.get("message", "target could not be started"))
            error_number = response.get("errno")
            if error_number is None:
                return OSError(message)
            return OSError(int(error_number), message)
        except (TypeError, ValueError):
            return OSError("Herdr worker could not start the target command")

    async def _wait_started(self) -> None:
        await self._launch_result

    async def _abort(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        if not self._reader_task.done():
            self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader_task
        if self._wait_result.done():
            with contextlib.suppress(BaseException):
                self._wait_result.exception()

    async def wait(self) -> int:
        result = await self._wait_result
        await self._reader_task
        return result

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._stdin is None:
            if input is not None:
                raise ValueError("Herdr run was not launched with PIPE stdin")
        else:
            if input is not None:
                self._stdin.write(input)
            self._stdin.close()
            await self._stdin.drain()
        stdout_task = asyncio.create_task(self._stdout.read())
        stderr_task = asyncio.create_task(self._stderr.read())
        try:
            await self.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            return stdout, stderr
        except BaseException:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

    async def cancel(self) -> None:
        if self._returncode is None:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                await self._send(CANCEL)
        with contextlib.suppress(HerdrUnavailableError):
            await self.wait()
        await self._cleanup()

    def release(self) -> None:
        if self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(self._release_async())

    async def wait_release(self) -> None:
        """Wait for the asynchronous resources requested by :meth:`release` to close."""
        if self._cleanup_task is not None:
            await self._cleanup_task

    async def _release_async(self) -> None:
        if self._returncode is None:
            await self.cancel()
        else:
            await self._cleanup()

    async def _cleanup(self) -> None:
        if self._cleanup_started:
            return
        self._cleanup_started = True
        try:
            if self._stdin is not None and not self._stdin.is_closing():
                self._stdin.close()
                await self._stdin.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        try:
            await _close_pane_with_timeout(
                self._client, self._pane, self._cleanup_timeout
            )
        finally:
            shutil.rmtree(self._run_dir, ignore_errors=True)

    def __del__(self) -> None:
        writer = getattr(self, "_writer", None)
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


class HerdrExecutionHost:
    """Launch one-shot commands in uniquely owned Herdr panes.

    .. warning::
       This execution host is experimental. Its configuration and lifecycle contract may change
       in a later minor release.
    """

    def __init__(
        self,
        *,
        client: HerdrClient | None = None,
        herdr_command: str | Sequence[str] = "herdr",
        session: str | None = None,
        startup_timeout: float = 15.0,
        cleanup_timeout: float = 5.0,
    ) -> None:
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be positive")
        self._herdr_command = (
            [herdr_command]
            if isinstance(herdr_command, str)
            else list(herdr_command)
        )
        if not self._herdr_command:
            raise ValueError("herdr_command must not be empty")
        self._session = session
        self._client = (
            client
            if client is not None
            else _CliHerdrClient(
                herdr_command=herdr_command,
                session=session,
                cleanup_timeout=cleanup_timeout,
            )
        )
        self._startup_timeout = startup_timeout
        self._cleanup_timeout = cleanup_timeout

    async def launch(
        self,
        command: list[str],
        cwd: str,
        *,
        env: dict[str, str] | None = None,
        stdin: int = asyncio.subprocess.DEVNULL,
        isolation_policy: IsolationPolicy | None = None,
    ) -> RunHandle:
        if isolation_policy is not None and not isinstance(isolation_policy, NoIsolation):
            raise IsolationUnavailableError(
                "HerdrExecutionHost supports only NoIsolation in v1"
            )
        if os.name == "nt":
            raise HerdrUnavailableError(
                "HerdrExecutionHost requires Unix-domain socket support"
            )
        if stdin not in (asyncio.subprocess.DEVNULL, asyncio.subprocess.PIPE):
            raise ValueError(
                "HerdrExecutionHost supports only DEVNULL or PIPE stdin"
            )
        if not command:
            raise ValueError("command must not be empty")

        cwd = os.path.abspath(cwd)
        run_dir = tempfile.mkdtemp(prefix="agentshell-herdr-")
        os.chmod(run_dir, 0o700)
        socket_path = os.path.join(run_dir, "bridge.sock")
        connection = asyncio.get_running_loop().create_future()

        async def accept(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            if connection.done():
                writer.close()
                await writer.wait_closed()
            else:
                connection.set_result((reader, writer))

        server: asyncio.Server | None = None
        pane: HerdrPane | None = None
        bridge_writer: asyncio.StreamWriter | None = None
        run: _HerdrRunHandle | None = None
        try:
            server = await asyncio.start_unix_server(accept, path=socket_path)
            worker_argv = [
                os.path.abspath(sys.executable),
                "-m",
                "agent_shell.herdr_worker",
                "--socket",
                socket_path,
            ]
            try:
                pane = await asyncio.wait_for(
                    self._client.create_pane(
                        cwd=cwd,
                        argv=worker_argv,
                        label=f"agentshell-{uuid.uuid4().hex}",
                    ),
                    timeout=self._startup_timeout,
                )
            except TimeoutError as error:
                raise HerdrUnavailableError(
                    "Herdr startup timed out while creating the Herdr pane"
                ) from error
            try:
                reader, bridge_writer = await asyncio.wait_for(
                    connection,
                    timeout=self._startup_timeout,
                )
            except TimeoutError as error:
                raise HerdrUnavailableError(
                    "Herdr startup timed out waiting for the Herdr bridge connection"
                ) from error
            try:
                hello_kind, hello_payload = await asyncio.wait_for(
                    read_frame(reader),
                    timeout=self._startup_timeout,
                )
            except TimeoutError as error:
                raise HerdrUnavailableError(
                    "Herdr startup timed out waiting for the bridge hello"
                ) from error
            if hello_kind != HELLO:
                raise HerdrUnavailableError("Herdr bridge did not send a hello frame")
            hello = json.loads(hello_payload.decode("utf-8"))
            pid = int(hello["pid"])
            run = _HerdrRunHandle(
                pid=pid,
                pane=pane,
                client=self._client,
                reader=reader,
                writer=bridge_writer,
                stdin_pipe=stdin == asyncio.subprocess.PIPE,
                run_dir=run_dir,
                cleanup_timeout=self._cleanup_timeout,
            )
            if isinstance(self._client, _CliHerdrClient):
                await run._send(
                    BRIDGE_CONFIG,
                    json.dumps(
                        {
                            "pane_id": pane.pane_id,
                            "workspace_id": pane.workspace_id,
                            "herdr_command": self._herdr_command,
                            "session": self._session,
                            "cleanup_timeout": self._cleanup_timeout,
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            await run._send(
                LAUNCH,
                json.dumps(
                    {
                        "command": list(command),
                        "cwd": cwd,
                        "env": None if env is None else dict(env),
                        "stdin": (
                            "pipe"
                            if stdin == asyncio.subprocess.PIPE
                            else "devnull"
                        ),
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            try:
                await asyncio.wait_for(
                    run._wait_started(), timeout=self._startup_timeout
                )
            except TimeoutError as error:
                raise HerdrUnavailableError(
                    "Herdr startup timed out waiting for the target to start"
                ) from error
            return run
        except BaseException:
            if run is not None:
                with contextlib.suppress(Exception):
                    await run._abort()
            elif bridge_writer is not None:
                bridge_writer.close()
                with contextlib.suppress(Exception):
                    await bridge_writer.wait_closed()
            try:
                if pane is not None:
                    await _close_pane_with_timeout(
                        self._client, pane, self._cleanup_timeout
                    )
            finally:
                shutil.rmtree(run_dir, ignore_errors=True)
            raise
        finally:
            if server is not None:
                server.close()
