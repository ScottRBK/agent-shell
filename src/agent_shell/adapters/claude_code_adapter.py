import asyncio
import codecs
import json
import os
import logging
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType, HealthCheckResult
from agent_shell.process_cleanup import register_process_group, unregister_process_group
from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.stderr_format import format_stderr
from agent_shell.adapters.tool_denial import resolve_disallowed_tools

logger = logging.getLogger("agent_shell.claude_code_adapter")

# Canonical deny-name -> Claude Code native tool names. `edit` fans out to the full
# file-modification family; everything else is one-to-one.
_DISALLOWED_TOOL_MAP = {
    "bash": ["Bash"],
    "edit": ["Edit", "Write", "NotebookEdit"],
    "read": ["Read"],
    "web_search": ["WebSearch"],
    "web_fetch": ["WebFetch"],
}

class ClaudeCodeAdapter():
    def __init__(self):
        self._active_processes = []

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
        chunks: list[StreamEvent] = []
        async for event in self.stream(
            cwd=cwd,
            prompt=prompt,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model=model,
            effort=effort,
            include_thinking=include_thinking,
            auto_approve=auto_approve,
            session_id=session_id,
        ):
            chunks.append(event)

        text = "\n".join(e.content for e in chunks if e.type == "text")
        cost = next((e.cost for e in reversed(chunks) if e.type == "result"), 0.0)
        duration = next((e.duration for e in reversed(chunks) if e.type == "result"), 0.0)
        output_tokens = next((e.output_tokens for e in reversed(chunks) if e.type == "result"), 0)
        returned_session_id = next((e.session_id for e in chunks if e.session_id), None)
        return AgentResponse(
            response=text, cost=cost, session_id=returned_session_id,
            duration=duration, output_tokens=output_tokens,
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
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]

        if auto_approve:
            cmd.append("--dangerously-skip-permissions")

        if allowed_tools:
            cmd.extend(["--allowed-tools", ",".join(allowed_tools)])

        # Deny-list. `--disallowed-tools` takes precedence over both --allowed-tools
        # and --dangerously-skip-permissions, so it is safe alongside auto_approve.
        native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
        if unsupported:
            warnings.warn(
                f"Claude Code cannot deny {unsupported}; ignoring",
                UserWarning,
                stacklevel=2,
            )
        if native:
            cmd.extend(["--disallowed-tools", ",".join(native)])

        if model:
            cmd.extend(["--model", model])

        if effort:
            cmd.extend(["--effort", effort])

        if session_id:
            cmd.extend(["--resume", session_id])

        logger.debug("Command: %s", cmd)
        logger.info("Process started (cwd=%s)", os.path.abspath(cwd))

        process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.abspath(cwd),
                preexec_fn=os.setsid,
        )

        self._active_processes.append(process)
        # setsid makes the child a session leader, so pgid == pid
        register_process_group(process.pid)

        # Drain stderr concurrently with stdout. Reading it only after the stdout loop can
        # deadlock: a child that fills its stderr pipe buffer (~64KB) mid-run blocks on that
        # write, never closes stdout, and our stdout read() then waits forever.
        stderr_task = asyncio.ensure_future(process.stderr.read())

        # Incremental decoder so a multibyte char split across two reads is stitched back
        # together instead of raising UnicodeDecodeError; "replace" keeps a truly truncated
        # tail from aborting the run.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
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
                                event=raw,
                                include_thinking=include_thinking
                                ):
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
                                event=raw,
                                include_thinking=include_thinking):
                                yield event
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed JSON: %s", line[:200])

            await process.wait()
            if process in self._active_processes:
                self._active_processes.remove(process)
            unregister_process_group(process.pid)

            stderr = await stderr_task
            if stderr and process.returncode != 0:
                error_msg = format_stderr(stderr)
                logger.warning("Process exited with code %d: %s", process.returncode, error_msg)
                yield StreamEvent(type="error", content=error_msg)
        finally:
            # On early consumer close (GeneratorExit at a yield) or any error, the concurrent
            # drain above is never awaited; cancel it so it is not left pending.
            if not stderr_task.done():
                stderr_task.cancel()

    def _parse_event(self, event: dict, include_thinking: bool) -> list[StreamEvent]:
        t = event.get("type", "")
        session_id = event.get("session_id")
        events = []

        if t == "system":
            if session_id:
                logger.info("Session: %s", session_id)
                events.append(StreamEvent(type="system", content="", session_id=session_id))

        elif t == "assistant":
            for item in event.get("message", {}).get("content", []):
                if item.get("type") == "text":
                    events.append(StreamEvent(type="text", content=item["text"]))
                elif item.get("type") == "tool_use":
                    tool_name = item.get("name", "")
                    logger.info("Tool call: %s", tool_name)
                    events.append(StreamEvent(type="tool_use", content=tool_name))
                elif item.get("type") == "thinking" and include_thinking:
                    events.append(StreamEvent(type="thinking", content=item.get("thinking", "")))

        elif t == "result":
            cost = event.get("total_cost_usd", 0) or 0
            duration = (event.get("duration_ms", 0) or 0) / 1000
            # Claude's result.usage.output_tokens is already cumulative for the run.
            # `or {}` tolerates a present-but-null usage; `or 0` a null token field.
            output_tokens = (event.get("usage") or {}).get("output_tokens", 0) or 0
            is_error = event.get("is_error", False)
            status = "error" if is_error else "ok"
            logger.info("Result: %s (cost=$%.4f, duration=%.1fs)", status, cost, duration)
            events.append(StreamEvent(
                type="result", content=status, cost=cost, duration=duration,
                session_id=session_id, output_tokens=output_tokens,
            ))

        return events

    async def cancel(self) -> None:
        for process in self._active_processes:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, 9)
                unregister_process_group(pgid)
            except ProcessLookupError:
                pass
        self._active_processes.clear()

    async def health_check(
            self,
            cwd: str,
            model: str | None = None,
            timeout: float = 60.0,
    ) -> HealthCheckResult:
        return await run_health_probe(self, cwd, model=model, timeout=timeout)

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        # Pre-remove for overwrite semantics; ignore failure (server may not exist).
        await self._run_mcp_command(
            ["claude", "mcp", "remove", "--scope", "user", mcp_server.name],
            raise_on_error=False,
        )

        cmd = ["claude", "mcp", "add"]

        if mcp_server.type == MCPServerType.STDIO:
            for key, value in mcp_server.env.items():
                cmd.extend(["-e", f"{key}={value}"])
            # Claude's --env option is variadic. A following option terminates its
            # values so it cannot consume the server name as another env value.
            cmd.extend(["--scope", "user", "--transport", mcp_server.type.value])
            cmd.append(mcp_server.name)
            cmd.append("--")
            cmd.append(mcp_server.command)
            cmd.extend(mcp_server.args)
        else:
            for key, value in mcp_server.headers.items():
                cmd.extend(["--header", f"{key}: {value}"])
            # --header is variadic for the same reason as --env above.
            cmd.extend(["--scope", "user", "--transport", mcp_server.type.value])
            cmd.append(mcp_server.name)
            cmd.append(mcp_server.url)

        await self._run_mcp_command(cmd, raise_on_error=True)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        cmd = ["claude", "mcp", "remove", "--scope", "user", mcp_server_name]
        result = await self._run_mcp_command(cmd, raise_on_error=False)
        if result["returncode"] != 0:
            warnings.warn(
                f"Could not remove MCP server '{mcp_server_name}': {result['stderr']}",
                UserWarning,
                stacklevel=2,
            )

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        config_path = Path(os.path.expanduser("~/.claude.json"))
        if not config_path.exists():
            return []

        config = json.loads(config_path.read_text())
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            warnings.warn(
                "Skipping malformed Claude Code 'mcpServers': expected object, "
                f"got {type(servers).__name__}",
                UserWarning,
                stacklevel=2,
            )
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

            entry_type = entry.get("type")
            if entry_type is None:
                if entry.get("command"):
                    entry_type = "stdio"
                elif entry.get("url"):
                    entry_type = "http"

            try:
                if entry_type == "stdio":
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.STDIO,
                        command=entry.get("command"),
                        args=list(entry.get("args") or []),
                        env=dict(entry.get("env") or {}),
                    ))
                elif entry_type in {"http", "sse"}:
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.HTTP,
                        url=entry.get("url"),
                        headers=dict(entry.get("headers") or {}),
                    ))
                else:
                    warnings.warn(
                        f"Skipping MCP entry '{name}': unsupported transport type "
                        f"{entry_type!r}",
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
                f"claude mcp command failed (exit {process.returncode}): {stderr_text.strip()}"
            )

        return {"returncode": process.returncode, "stdout": stdout_text, "stderr": stderr_text}

                


