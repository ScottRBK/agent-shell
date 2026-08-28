import asyncio
import codecs
import json
import logging
import os
import tomllib
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.model_discovery import decode_model_output, run_model_command
from agent_shell.adapters.process_failure import process_failure_event
from agent_shell.adapters.response import collect_response
from agent_shell.adapters.stderr_format import format_stderr
from agent_shell.adapters.tool_denial import resolve_disallowed_tools
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

logger = logging.getLogger("agent_shell.grok_adapter")


def _result_error_reason(errors) -> str | None:
    """Best-effort reason from a result event's optional errors[] list."""
    if not errors:
        return None
    if isinstance(errors, str):
        return errors or None
    if not isinstance(errors, list):
        return str(errors)
    parts: list[str] = []
    for item in errors:
        if isinstance(item, str) and item:
            parts.append(item)
        elif isinstance(item, dict):
            message = item.get("message") or item.get("error") or item.get("reason")
            if message:
                parts.append(str(message))
            else:
                parts.append(str(item))
        elif item is not None:
            parts.append(str(item))
    return "; ".join(parts) if parts else None

# Canonical deny-name -> ids accepted by `grok --disallowed-tools`.
#
# Measured on grok 1.0.0: system/init lists the shell tool as `run_terminal_command`,
# but `--disallowed-tools run_terminal_command` is a no-op. The working deny id is the
# shorter `run_terminal_cmd` (matches ~/.grok/docs headless guide). `edit` fans out
# across the file-modification family; those deny ids match init.tools 1:1.
_DISALLOWED_TOOL_MAP = {
    "bash": ["run_terminal_cmd"],
    "edit": ["search_replace", "write"],
    "read": ["read_file"],
    "web_search": ["web_search"],
    "web_fetch": ["web_fetch"],
}


class GrokAdapter:
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
        native, unsupported = resolve_disallowed_tools(
            disallowed_tools, _DISALLOWED_TOOL_MAP,
        )
        if unsupported:
            warnings.warn(
                f"Grok cannot deny {unsupported}; ignoring",
                UserWarning,
                stacklevel=2,
            )

        cmd = self._build_command(
            prompt=prompt,
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            session_id=session_id,
            allowed_tools=allowed_tools,
            disallowed_native=native,
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
                            for event in self._parse_event(
                                    raw, include_thinking=include_thinking):
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
                            for event in self._parse_event(
                                    raw, include_thinking=include_thinking):
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
            await release_process(
                process, self._active_processes, stderr_task, child_exited=child_exited,
            )

    def _build_command(
            self,
            prompt: str,
            model: str | None,
            effort: str | None,
            auto_approve: bool,
            session_id: str | None,
            allowed_tools: list[str] | None,
            disallowed_native: list[str],
    ) -> list[str]:
        # streaming-messages-json is the Anthropic Messages wire format: full assistant
        # blocks (not token deltas). streaming-json emits text fragments that would break
        # execute()'s "\n".join over text events (same class of bug as issue #6 / pi
        # text_end / copilot assistant.message). Prefer complete blocks.
        cmd = ["grok", "-p", prompt, "--output-format", "streaming-messages-json"]

        if auto_approve:
            cmd.append("--always-approve")

        if model:
            cmd.extend(["-m", model])

        if effort:
            cmd.extend(["--reasoning-effort", effort])

        if session_id:
            cmd.extend(["--resume", session_id])

        if allowed_tools:
            cmd.extend(["--tools", ",".join(allowed_tools)])

        if disallowed_native:
            cmd.extend(["--disallowed-tools", ",".join(disallowed_native)])

        return cmd

    def _parse_event(self, event: dict, include_thinking: bool) -> list[StreamEvent]:
        t = event.get("type", "")
        events: list[StreamEvent] = []

        if t == "system" and event.get("subtype") == "init":
            # Session-id carrier (same shape as Claude Code / Cursor).
            session_id = event.get("session_id")
            if session_id:
                logger.info("Session: %s", session_id)
                events.append(StreamEvent(type="system", content="", session_id=session_id))

        elif t == "assistant":
            # Full content blocks (not per-token deltas) — safe for execute()'s newline join.
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        events.append(StreamEvent(type="text", content=text))
                elif btype == "thinking" and include_thinking:
                    text = block.get("thinking") or ""
                    if text:
                        events.append(StreamEvent(type="thinking", content=text))
                elif btype in {"tool_use", "server_tool_use"}:
                    # server_tool_use is Grok's inline web-search block shape.
                    name = block.get("name") or "tool"
                    logger.info("Tool call: %s", name)
                    events.append(StreamEvent(type="tool_use", content=name))

        elif t == "result":
            is_error = event.get("is_error", False)
            status = "error" if is_error else "ok"
            usage = event.get("usage") or {}
            # Grok's output_tokens already includes reasoning (reasoning_tokens is a
            # subset when present — total_tokens math uses output_tokens alone). Do NOT
            # add them or billed output double-counts.
            output_tokens = int(usage.get("output_tokens") or 0)
            cost = float(event.get("total_cost_usd") or 0.0)
            duration = (event.get("duration_ms", 0) or 0) / 1000
            session_id = event.get("session_id")
            # Structured failure reasons ride on result.errors[] when present.
            error_reason = _result_error_reason(event.get("errors"))
            logger.info(
                "Result: %s (cost=%.6f, duration=%.1fs, output_tokens=%d)",
                status, cost, duration, output_tokens,
            )
            events.append(StreamEvent(
                type="result",
                content=status,
                cost=cost,
                duration=duration,
                session_id=session_id,
                output_tokens=output_tokens,
                error=error_reason,
            ))

        # user echoes, tool_result carriers, unknowns: ignore.

        return events

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
        cmd = ["grok", "models"]
        returncode, stdout, stderr = await run_model_command(cmd, cwd, timeout)
        if returncode != 0:
            message = format_stderr(stderr) or f"exit code {returncode}"
            raise RuntimeError(f"`grok models` failed: {message}")

        output = decode_model_output(stdout, "`grok models`")
        return self._parse_models_output(output)

    def _parse_models_output(self, output: str) -> list[str]:
        # Plain text, e.g.:
        #   Default model: grok-4.5
        #   Available models:
        #     * grok-4.5 (default)
        models: list[str] = []
        in_list = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Available models"):
                in_list = True
                continue
            if not in_list or not stripped:
                continue
            name = stripped.lstrip("* ").split("(")[0].strip()
            if name:
                models.append(name)

        if not in_list:
            raise RuntimeError("Unexpected `grok models` output")
        return models

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        # `grok mcp add` is already add-or-update for the chosen scope. Do NOT pre-remove:
        # unscoped remove searches user AND project and can delete a project-owned server
        # with the same name (AGENTS.md user-scope contract).
        cmd = ["grok", "mcp", "add", "--scope", "user"]

        if mcp_server.type == MCPServerType.STDIO:
            for key, value in mcp_server.env.items():
                cmd.extend(["-e", f"{key}={value}"])
            cmd.extend(["--transport", "stdio", mcp_server.name, "--"])
            cmd.append(mcp_server.command)
            cmd.extend(mcp_server.args)
        else:
            for key, value in mcp_server.headers.items():
                cmd.extend(["--header", f"{key}: {value}"])
            cmd.extend([
                "--transport", "http",
                mcp_server.name,
                mcp_server.url,
            ])

        await self._run_mcp_command(cmd, raise_on_error=True)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        # Always pin user scope — unscoped remove can hit project config.
        result = await self._run_mcp_command(
            ["grok", "mcp", "remove", "--scope", "user", mcp_server_name],
            raise_on_error=False,
        )
        if result["returncode"] != 0:
            warnings.warn(
                f"Could not remove MCP server '{mcp_server_name}': {result['stderr']}",
                UserWarning,
                stacklevel=2,
            )

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        # User-scope config is TOML at ~/.grok/config.toml. Read it directly so listing
        # does not launch servers (grok mcp list is human-oriented).
        config_path = Path(os.path.expanduser("~/.grok/config.toml"))
        if not config_path.exists():
            return []

        try:
            config = tomllib.loads(config_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as e:
            warnings.warn(
                f"Could not read Grok MCP config: {e}",
                UserWarning,
                stacklevel=2,
            )
            return []

        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict):
            warnings.warn(
                "Skipping malformed Grok 'mcp_servers': expected table, "
                f"got {type(servers).__name__}",
                UserWarning,
                stacklevel=2,
            )
            return []

        result: list[MCPServerSpec] = []
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': expected table, "
                    f"got {type(entry).__name__}",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            try:
                if entry.get("command"):
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.STDIO,
                        command=entry.get("command"),
                        args=list(entry.get("args") or []),
                        env=dict(entry.get("env") or {}),
                    ))
                elif entry.get("url"):
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.HTTP,
                        url=entry.get("url"),
                        headers=dict(entry.get("headers") or {}),
                    ))
                else:
                    warnings.warn(
                        f"Skipping MCP entry '{name}': no command or url",
                        UserWarning,
                        stacklevel=2,
                    )
            except (TypeError, ValueError) as e:
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': {e}",
                    UserWarning,
                    stacklevel=2,
                )

        return result

    async def _run_mcp_command(self, cmd: list[str], raise_on_error: bool) -> dict:
        logger.debug("MCP command: %s", cmd)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8") if stdout else ""
        stderr_text = stderr.decode("utf-8") if stderr else ""

        if process.returncode != 0 and raise_on_error:
            raise RuntimeError(
                f"grok mcp command failed (exit {process.returncode}): "
                f"{stderr_text.strip() or stdout_text.strip()}"
            )

        return {
            "returncode": process.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }
