import asyncio
import json
import os
import logging
import warnings
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType
from agent_shell.process_cleanup import register_process_group, unregister_process_group

logger = logging.getLogger("agent_shell.claude_code_adapter")

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
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]

        if auto_approve:
            cmd.append("--dangerously-skip-permissions")

        if allowed_tools:
            cmd.extend(["--allowed-tools", ",".join(allowed_tools)])

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

        buffer = ""
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
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

            buffer += chunk.decode("utf-8")
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

        stderr = await process.stderr.read()
        if stderr and process.returncode != 0:
            error_msg = stderr.decode("utf-8")[-500:]
            logger.warning("Process exited with code %d: %s", process.returncode, error_msg)
            yield StreamEvent(type="error", content=error_msg)

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
            is_error = event.get("is_error", False)
            status = "error" if is_error else "ok"
            logger.info("Result: %s (cost=$%.4f, duration=%.1fs)", status, cost, duration)
            events.append(StreamEvent(type="result", content=status, cost=cost, duration=duration, session_id=session_id))

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
        # Pre-remove for overwrite semantics; ignore failure (server may not exist).
        await self._run_mcp_command(
            ["claude", "mcp", "remove", "--scope", "user", mcp_server.name],
            raise_on_error=False,
        )

        cmd = ["claude", "mcp", "add", "--scope", "user", "--transport", mcp_server.type.value]

        if mcp_server.type == MCPServerType.STDIO:
            for key, value in mcp_server.env.items():
                cmd.extend(["-e", f"{key}={value}"])
            cmd.append(mcp_server.name)
            cmd.append("--")
            cmd.append(mcp_server.command)
            cmd.extend(mcp_server.args)
        else:
            for key, value in mcp_server.headers.items():
                cmd.extend(["--header", f"{key}: {value}"])
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
        # Claude Code's `claude mcp list` outputs a flat summary, not full specs.
        # Reconstruction would require parsing per-server `claude mcp get` output
        # or reading ~/.claude.json directly. Tracked separately.
        raise NotImplementedError(
            "list_mcp_servers is not yet implemented for Claude Code"
        )

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

                




