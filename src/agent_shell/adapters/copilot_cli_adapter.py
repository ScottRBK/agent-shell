import asyncio
import json
import logging
import os
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType
from agent_shell.process_cleanup import register_process_group, unregister_process_group

logger = logging.getLogger("agent_shell.copilot_cli_adapter")


class CopilotCLIAdapter:
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
            "copilot", "-p", prompt,
            "--output-format", "json",
            "--silent",
        ]

        if auto_approve:
            cmd.append("--allow-all-tools")

        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allow-tool", tool])

        if model:
            cmd.extend(["--model", model])

        if effort:
            cmd.extend(["--effort", effort])

        if session_id:
            cmd.extend(["--resume", session_id])

        if include_thinking:
            cmd.append("--enable-reasoning-summaries")

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
                        for event in self._parse_event(
                            event=raw,
                            include_thinking=include_thinking,
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
                            include_thinking=include_thinking,
                        ):
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
        events = []

        if t == "assistant.reasoning_delta" and include_thinking:
            delta_content = event.get("data", {}).get("deltaContent", "")
            if delta_content:
                events.append(StreamEvent(type="thinking", content=delta_content))

        elif t == "assistant.reasoning" and include_thinking:
            content = event.get("data", {}).get("content", "") or event.get("content", "")
            if content:
                events.append(StreamEvent(type="thinking", content=content))

        elif t == "assistant.message_delta":
            delta_content = event.get("data", {}).get("deltaContent", "")
            if delta_content:
                events.append(StreamEvent(type="text", content=delta_content))

        elif t == "assistant.message":
            tool_requests = event.get("data", {}).get("toolRequests", [])
            for tool in tool_requests:
                tool_name = tool.get("name", "")
                logger.info("Tool call: %s", tool_name)
                events.append(StreamEvent(type="tool_use", content=tool_name))

        elif t == "result":
            exit_code = event.get("exitCode", 0)
            status = "ok" if exit_code == 0 else "error"
            usage = event.get("usage", {})
            duration = (usage.get("totalApiDurationMs", 0) or 0) / 1000
            session_id = event.get("sessionId")
            logger.info("Result: %s (duration=%.1fs)", status, duration)
            events.append(StreamEvent(
                type="result",
                content=status,
                cost=0.0,
                duration=duration,
                session_id=session_id,
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

    def _config_path(self) -> Path:
        return Path(os.path.expanduser("~/.copilot/mcp-config.json"))

    def _read_config(self) -> dict:
        path = self._config_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _write_config(self, config: dict) -> None:
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2))

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        config = self._read_config()
        config.setdefault("mcpServers", {})

        if mcp_server.type == MCPServerType.STDIO:
            entry = {
                "type": "local",
                "command": mcp_server.command,
                "args": list(mcp_server.args),
                "env": dict(mcp_server.env),
                "tools": ["*"],
            }
        else:
            entry = {
                "type": "http",
                "url": mcp_server.url,
                "headers": dict(mcp_server.headers),
                "tools": ["*"],
            }

        config["mcpServers"][mcp_server.name] = entry
        self._write_config(config)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        config = self._read_config()
        servers = config.get("mcpServers", {})
        if mcp_server_name not in servers:
            warnings.warn(
                f"MCP server '{mcp_server_name}' not found in Copilot CLI config",
                UserWarning,
                stacklevel=2,
            )
            return

        del servers[mcp_server_name]
        config["mcpServers"] = servers
        self._write_config(config)

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        config = self._read_config()
        servers = config.get("mcpServers", {})
        result: list[MCPServerSpec] = []

        for name, entry in servers.items():
            entry_type = entry.get("type")
            if entry_type == "local":
                result.append(MCPServerSpec(
                    name=name,
                    type=MCPServerType.STDIO,
                    command=entry.get("command"),
                    args=list(entry.get("args", [])),
                    env=dict(entry.get("env", {})),
                ))
            elif entry_type == "http":
                result.append(MCPServerSpec(
                    name=name,
                    type=MCPServerType.HTTP,
                    url=entry.get("url"),
                    headers=dict(entry.get("headers", {})),
                ))

        return result
