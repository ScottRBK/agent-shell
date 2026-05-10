import asyncio
import json
import logging
import os
import warnings
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType
from agent_shell.process_cleanup import register_process_group, unregister_process_group

logger = logging.getLogger("agent_shell.codex_adapter")


class CodexAdapter:
    def __init__(self):
        self._active_processes = []
        self._warned_include_thinking = False
        self._warned_allowed_tools = False

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
    ) -> AgentResponse:
        chunks: list[StreamEvent] = []
        async for event in self.stream(
            cwd=cwd,
            prompt=prompt,
            allowed_tools=allowed_tools,
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
        returned_session_id = next((e.session_id for e in chunks if e.session_id), None)
        return AgentResponse(response=text, cost=cost, session_id=returned_session_id, duration=duration)

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
    ) -> AsyncIterator[StreamEvent]:
        if include_thinking and not self._warned_include_thinking:
            warnings.warn(
                "Codex --json does not stream reasoning items; include_thinking has no effect",
                UserWarning,
                stacklevel=2,
            )
            self._warned_include_thinking = True

        if allowed_tools and not self._warned_allowed_tools:
            warnings.warn(
                "Codex CLI has no per-call allowed_tools mechanism; ignoring",
                UserWarning,
                stacklevel=2,
            )
            self._warned_allowed_tools = True

        cmd = self._build_command(
            prompt=prompt,
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            session_id=session_id,
        )

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
        register_process_group(process.pid)

        buffer = ""
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                if buffer.strip():
                    try:
                        raw = json.loads(buffer)
                        logger.debug("Raw event: %s", raw)
                        for event in self._parse_event(raw):
                            yield event
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed JSON: %s", buffer[:200])
                break

            buffer += chunk.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    try:
                        raw = json.loads(line)
                        logger.debug("Raw event: %s", raw)
                        for event in self._parse_event(raw):
                            yield event
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed JSON: %s", line[:200])

        await process.wait()
        if process in self._active_processes:
            self._active_processes.remove(process)
        unregister_process_group(process.pid)

        stderr = await process.stderr.read()
        if stderr and process.returncode != 0:
            error_msg = stderr.decode("utf-8")[-500:]
            logger.warning("Process exited with code %d: %s", process.returncode, error_msg)
            yield StreamEvent(type="error", content=error_msg)

    def _build_command(
            self,
            prompt: str,
            model: str | None,
            effort: str | None,
            auto_approve: bool,
            session_id: str | None,
    ) -> list[str]:
        if session_id:
            cmd = ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
            if model:
                cmd.extend(["--model", model])
            if effort:
                cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
            cmd.extend([session_id, prompt])
            return cmd

        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write"]
        if auto_approve:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        cmd.append(prompt)
        return cmd

    def _parse_event(self, event: dict) -> list[StreamEvent]:
        t = event.get("type", "")
        events: list[StreamEvent] = []

        if t == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                events.append(StreamEvent(type="session", content="", session_id=thread_id))

        elif t == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    events.append(StreamEvent(type="text", content=text))
            elif item_type == "command_execution":
                command = item.get("command", "")
                logger.info("Tool call: command_execution %s", command)
                events.append(StreamEvent(type="tool_use", content=command))

        elif t == "turn.completed":
            events.append(StreamEvent(type="result", content="ok", cost=0.0, duration=0.0))

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

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        if mcp_server.type == MCPServerType.STDIO:
            cmd = ["codex", "mcp", "add", mcp_server.name]
            for key, value in mcp_server.env.items():
                cmd.extend(["--env", f"{key}={value}"])
            cmd.append("--")
            cmd.append(mcp_server.command)
            cmd.extend(mcp_server.args)
        else:
            if mcp_server.headers:
                warnings.warn(
                    f"Codex MCP add does not accept arbitrary HTTP headers; "
                    f"ignoring headers for '{mcp_server.name}'",
                    UserWarning,
                    stacklevel=2,
                )
            cmd = ["codex", "mcp", "add", mcp_server.name, "--url", mcp_server.url]

        await self._run_codex_mcp(cmd)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        cmd = ["codex", "mcp", "remove", mcp_server_name]
        stdout, _ = await self._run_codex_mcp(cmd)
        # Codex returns exit 0 with this message when the server didn't exist.
        if "No MCP server named" in stdout:
            warnings.warn(
                f"MCP server '{mcp_server_name}' not found in Codex config",
                UserWarning,
                stacklevel=2,
            )

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        cmd = ["codex", "mcp", "list", "--json"]
        stdout, _ = await self._run_codex_mcp(cmd)

        try:
            entries = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse `codex mcp list --json` output: {e}") from e

        result: list[MCPServerSpec] = []
        for entry in entries:
            name = entry.get("name", "<unnamed>")
            transport = entry.get("transport") or {}
            transport_type = transport.get("type")

            try:
                if transport_type == "stdio":
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.STDIO,
                        command=transport.get("command"),
                        args=list(transport.get("args") or []),
                        env=dict(transport.get("env") or {}),
                    ))
                elif transport_type == "streamable_http":
                    # Note: bearer_token_env_var and http_headers from codex are
                    # not round-tripped through MCPServerSpec.
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.HTTP,
                        url=transport.get("url"),
                    ))
                else:
                    warnings.warn(
                        f"Skipping MCP entry '{name}': unknown transport type "
                        f"{transport_type!r}",
                        UserWarning,
                        stacklevel=2,
                    )
            except ValueError as e:
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': {e}",
                    UserWarning,
                    stacklevel=2,
                )

        return result

    async def _run_codex_mcp(self, cmd: list[str]) -> tuple[str, str]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8") if stderr_bytes else ""
        if process.returncode != 0:
            message = stderr.strip() or stdout.strip() or f"exit code {process.returncode}"
            raise RuntimeError(f"`{' '.join(cmd)}` failed: {message}")
        return stdout, stderr
