import asyncio
import codecs
import json
import logging
import os
import warnings
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import AsyncIterator

from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.model_discovery import decode_model_output, run_model_command
from agent_shell.adapters.process_failure import process_failure_event
from agent_shell.adapters.response import collect_response
from agent_shell.adapters.stderr_format import format_stderr
from agent_shell.execution import (
    ExecutionHost,
    IsolationPolicy,
    NativeExecutionHost,
    NoIsolation,
)
from agent_shell.models.agent import (
    AgentResponse,
    HealthCheckResult,
    MCPServerSpec,
    MCPServerType,
    StreamEvent,
)
from agent_shell.process_cleanup import (
    release_process,
)

logger = logging.getLogger("agent_shell.cursor_adapter")


class CursorAdapter:
    def __init__(
            self,
            execution_host: ExecutionHost | None = None,
            isolation_policy: IsolationPolicy | None = None,
    ):
        self._active_processes = []
        self._execution_host = (
            execution_host if execution_host is not None else NativeExecutionHost()
        )
        self._isolation_policy = (
            isolation_policy if isolation_policy is not None else NoIsolation()
        )
        self._warned_allowed_tools = False
        self._warned_effort = False

    async def execute(
            self,
            cwd: str,
            prompt: str,
            allowed_tools: list[str] | None = None,
            model: str | None = None,
            effort: str | None = None,
            include_thinking: bool = False,
            auto_approve: bool = True,
            session_id: str | None = None,
            disallowed_tools: list[str] | None = None,
    ) -> AgentResponse:
        return await collect_response(
            self,
            cwd,
            prompt,
            allowed_tools=allowed_tools,
            model=model,
            effort=effort,
            include_thinking=include_thinking,
            auto_approve=auto_approve,
            session_id=session_id,
            disallowed_tools=disallowed_tools,
        )

    async def stream(
            self,
            cwd: str,
            prompt: str,
            allowed_tools: list[str] | None = None,
            model: str | None = None,
            effort: str | None = None,
            include_thinking: bool = False,
            auto_approve: bool = True,
            session_id: str | None = None,
            disallowed_tools: list[str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Cursor exposes NO per-call tool policy: allow/deny live in .cursor/cli.json only, and
        # there is no standalone effort flag (effort is a model bracket-override this adapter
        # does not inject). So allowed_tools/effort are informational (warn-once) and
        # disallowed_tools is unenforceable. include_thinking IS honoured — Cursor streams
        # reasoning as thinking deltas.
        if allowed_tools and not self._warned_allowed_tools:
            warnings.warn(
                "Cursor CLI has no per-call allowed_tools mechanism "
                "(tool policy lives in .cursor/cli.json); ignoring allowed_tools",
                UserWarning,
                stacklevel=2,
            )
            self._warned_allowed_tools = True

        if effort and not self._warned_effort:
            warnings.warn(
                "Cursor CLI has no effort flag (effort is only a model bracket-override); "
                "ignoring effort",
                UserWarning,
                stacklevel=2,
            )
            self._warned_effort = True

        if disallowed_tools:
            # Warn EVERY call (not warn-once like allowed_tools/effort above): a silently
            # dropped deny is a security hole, and a reused adapter instance may request a
            # different unenforceable deny on a later call. Cursor cannot enforce ANY deny
            # per call, so the whole list is reported.
            warnings.warn(
                f"Cursor CLI has no per-call deny mechanism; ignoring "
                f"disallowed_tools={sorted(set(disallowed_tools))}",
                UserWarning,
                stacklevel=2,
            )

        cmd = self._build_command(
            prompt=prompt,
            model=model,
            auto_approve=auto_approve,
            session_id=session_id,
        )

        logger.debug("Command: %s", cmd)
        logger.info("Process started (cwd=%s)", os.path.abspath(cwd))

        process = await self._execution_host.launch(
            cmd,
            cwd=os.path.abspath(cwd),
            isolation_policy=self._isolation_policy,
        )

        self._active_processes.append(process)

        # Drain stderr concurrently with stdout. Reading it only after the stdout loop can
        # deadlock: a child that fills its stderr pipe buffer (~64KB) mid-run blocks on that
        # write, never closes stdout, and our stdout read() then waits forever.
        stderr_task = asyncio.ensure_future(process.stderr.read())

        # Incremental decoder so a multibyte char split across two reads is stitched back
        # together instead of raising UnicodeDecodeError; "replace" keeps a truly truncated
        # tail from aborting the run.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        # Which exit path stream() took, for the teardown in the `finally`. Only the normal
        # path below sets it, so every other way out of this body counts as abandonment.
        child_exited = False
        try:
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    buffer += decoder.decode(b"", final=True)
                    if buffer.strip():
                        try:
                            raw = json.loads(buffer)
                            logger.debug("Raw event: %s", raw)
                            for event in self._parse_event(raw, include_thinking=include_thinking):
                                yield event
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed JSON: %s", buffer[:200])
                    break

                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        try:
                            raw = json.loads(line)
                            logger.debug("Raw event: %s", raw)
                            for event in self._parse_event(raw, include_thinking=include_thinking):
                                yield event
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed JSON: %s", line[:200])

            # stdout hit EOF, so the child has exited or is about to. Reap it here, then
            # record that we did: the teardown must not re-derive this from
            # process.returncode, which cannot tell "still running" from "already reaped and
            # its pid handed to someone else".
            await process.wait()
            child_exited = True

            stderr = await stderr_task
            failure = process_failure_event(process.returncode, stderr)
            if failure is not None:
                logger.warning("Process failed (%d): %s", process.returncode, failure.content)
                yield failure
        finally:
            # Teardown must live here, not after the read loop: on an exception, or when the
            # consumer abandons the stream, the normal path never runs and the still-running
            # child and its registry entry leaked (issue #7).
            #
            # On abandonment this is NOT synchronous with the consumer's `break`: CPython
            # schedules the generator's aclose() as a separate async_generator_athrow task, so
            # the child is still alive and still registered until a later turn of the loop, and
            # if the loop is torn down first (asyncio.run cancelling pending tasks) it never
            # runs at all. That last case is what the atexit net in process_cleanup covers.
            await release_process(process, self._active_processes, stderr_task,
                                  child_exited=child_exited)

    def _build_command(
            self,
            prompt: str,
            model: str | None,
            auto_approve: bool,
            session_id: str | None,
    ) -> list[str]:
        # --print + --output-format stream-json is the headless NDJSON surface. --trust is
        # MANDATORY: without it cursor-agent refuses to run in an untrusted dir (exit 1, a
        # plain-text "Workspace Trust Required" on stderr, zero stdout).
        cmd = ["cursor-agent", "--print", "--output-format", "stream-json", "--trust"]

        # auto_approve maps to --force (auto-run tools). Without it tools auto-reject but the
        # run still completes (exit 0); --trust already permits the run itself.
        if auto_approve:
            cmd.append("--force")

        if model:
            cmd.extend(["--model", model])

        # `--resume [chatId]` takes an OPTIONAL arg, so the '=' form is used to bind the id
        # unambiguously ahead of the positional prompt.
        if session_id:
            cmd.append(f"--resume={session_id}")

        # Prompt is a positional argument; keep it LAST.
        cmd.append(prompt)
        return cmd

    def _parse_event(self, event: dict, include_thinking: bool) -> list[StreamEvent]:
        t = event.get("type", "")
        events: list[StreamEvent] = []

        if t == "system" and event.get("subtype") == "init":
            # The init event is the session-id carrier. Emit nothing if there is no id.
            session_id = event.get("session_id")
            if session_id:
                logger.info("Session: %s", session_id)
                events.append(StreamEvent(type="system", content="", session_id=session_id))

        elif t == "thinking":
            # Reasoning arrives as deltas (the `completed` carrier has no text). Deltas are
            # safe to surface individually: execute() joins only `text` events, never thinking.
            if include_thinking and event.get("subtype") == "delta":
                text = event.get("text") or ""
                if text:
                    events.append(StreamEvent(type="thinking", content=text))

        elif t == "assistant":
            # Assistant messages carry FULL text blocks (not per-token deltas), so each block
            # is surfaced as a `text` event and execute() joins them with "\n".
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text") or ""
                    if text:
                        events.append(StreamEvent(type="text", content=text))

        elif t == "tool_call":
            # One tool_use per call, on `started` only (the `completed` event carries the
            # result, not the invocation).
            if event.get("subtype") == "started":
                name = self._tool_name(event.get("tool_call") or {})
                logger.info("Tool call: %s", name)
                events.append(StreamEvent(type="tool_use", content=name))

        elif t == "result":
            # One result per run. `is_error` gives the ok/error status; usage.outputTokens is
            # undocumented but real (a cost measure); there is no cost field. duration_ms -> s.
            is_error = event.get("is_error", False)
            status = "error" if is_error else "ok"
            output_tokens = (event.get("usage") or {}).get("outputTokens", 0) or 0
            duration = (event.get("duration_ms", 0) or 0) / 1000
            logger.info("Result: %s (duration=%.1fs, output_tokens=%d)",
                        status, duration, output_tokens)
            events.append(StreamEvent(
                type="result", content=status, cost=0.0, duration=duration,
                output_tokens=output_tokens,
            ))

        return events

    def _tool_name(self, tool_call: dict) -> str:
        """Best-effort identifier for a started tool call: the shell command, or the MCP
        tool's fully-qualified name."""
        shell = tool_call.get("shellToolCall")
        if shell is not None:
            return (shell.get("args") or {}).get("command") or "shell"

        mcp = tool_call.get("mcpToolCall")
        if mcp is not None:
            args = mcp.get("args") or {}
            return args.get("name") or args.get("toolName") or "mcp"

        return "tool"

    async def cancel(self) -> None:
        processes = list(self._active_processes)
        self._active_processes.clear()
        for process in processes:
            await process.cancel()

    async def health_check(
            self,
            cwd: str,
            model: str | None = None,
            timeout: float = 60.0,
    ) -> HealthCheckResult:
        return await run_health_probe(self, cwd, model=model, timeout=timeout)

    async def list_models(
            self,
            cwd: str,
            timeout: float = 30.0,
    ) -> list[str]:
        cmd = ["cursor-agent", "models"]
        returncode, stdout, stderr = await run_model_command(
            cmd,
            cwd,
            timeout,
        )
        if returncode != 0:
            message = format_stderr(stderr) or f"exit code {returncode}"
            raise RuntimeError(f"`cursor-agent models` failed: {message}")

        output = decode_model_output(stdout, "`cursor-agent models`")
        models: list[str] = []
        for line in output.splitlines():
            if (
                not line
                or line == "Available models"
                or line.startswith("Tip: use --model ")
            ):
                continue
            model, separator, _ = line.partition(" - ")
            if not separator or not model.strip():
                raise RuntimeError("Unexpected `cursor-agent models` output")
            models.append(model.strip())
        return models

    def _mcp_config_path(self) -> Path:
        return Path(os.path.expanduser("~/.cursor/mcp.json"))

    def _read_mcp_config(self) -> dict:
        path = self._mcp_config_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _write_mcp_config(self, config: dict) -> None:
        path = self._mcp_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None

        try:
            with NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(json.dumps(config, indent=2) + "\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _mcp_servers(config: dict) -> dict:
        if not isinstance(config, dict):
            raise ValueError("Cursor mcp.json root must be a JSON object")

        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("Cursor 'mcpServers' must be a JSON object")

        config["mcpServers"] = servers
        return servers

    @staticmethod
    def _mcp_entry_type(entry: dict) -> MCPServerType | None:
        entry_type = entry.get("type")
        if entry_type == "stdio":
            return MCPServerType.STDIO
        if entry_type in {"http", "sse"}:
            return MCPServerType.HTTP
        if entry_type is not None:
            return None
        if entry.get("command"):
            return MCPServerType.STDIO
        if entry.get("url"):
            return MCPServerType.HTTP
        return None

    @staticmethod
    def _string_list(value: object, field_name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{field_name} must be a list of strings")
        return list(value)

    @staticmethod
    def _string_mapping(value: object, field_name: str) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict) or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in value.items()
        ):
            raise TypeError(f"{field_name} must be an object with string values")
        return dict(value)

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        config = self._read_mcp_config()
        servers = self._mcp_servers(config)

        if mcp_server.type == MCPServerType.STDIO:
            entry = {
                "command": mcp_server.command,
                "args": list(mcp_server.args),
                "env": dict(mcp_server.env),
            }
        else:
            entry = {
                "url": mcp_server.url,
                "headers": dict(mcp_server.headers),
            }

        existing = servers.get(mcp_server.name)
        if (
                isinstance(existing, dict)
                and self._mcp_entry_type(existing) == mcp_server.type
        ):
            entry = {**existing, **entry}

        servers[mcp_server.name] = entry
        self._write_mcp_config(config)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        config = self._read_mcp_config()
        servers = self._mcp_servers(config)
        if mcp_server_name not in servers:
            warnings.warn(
                f"MCP server '{mcp_server_name}' not found in Cursor config",
                UserWarning,
                stacklevel=2,
            )
            return

        del servers[mcp_server_name]
        self._write_mcp_config(config)

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        config = self._read_mcp_config()
        try:
            servers = self._mcp_servers(config)
        except ValueError as error:
            warnings.warn(str(error), UserWarning, stacklevel=2)
            return []

        result: list[MCPServerSpec] = []
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': expected object, "
                    f"got {type(entry).__name__}",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            try:
                entry_type = self._mcp_entry_type(entry)
                if entry_type == MCPServerType.STDIO:
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.STDIO,
                        command=entry.get("command"),
                        args=self._string_list(entry.get("args"), "args"),
                        env=self._string_mapping(entry.get("env"), "env"),
                    ))
                elif entry_type == MCPServerType.HTTP:
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.HTTP,
                        url=entry.get("url"),
                        headers=self._string_mapping(entry.get("headers"), "headers"),
                    ))
                else:
                    warnings.warn(
                        f"Skipping malformed MCP entry '{name}': "
                        "unsupported or missing transport",
                        UserWarning,
                        stacklevel=2,
                    )
            except (TypeError, ValueError) as error:
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': {error}",
                    UserWarning,
                    stacklevel=2,
                )

        return result
