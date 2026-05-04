import asyncio
import json
import logging
import os
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType
from agent_shell.process_cleanup import register_process_group, unregister_process_group

logger = logging.getLogger("agent_shell.opencode_adapter")


class OpenCodeAdapter():
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
        cmd = ["opencode", "run", "--format", "json"]

        if model:
            cmd.extend(["-m", model])

        if session_id:
            cmd.extend(["-s", session_id])

        cmd.append(prompt)

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
        session_id = event.get("sessionID")
        events = []

        if t == "step_start":
            logger.info("Session: %s", session_id)
            events.append(StreamEvent(type="system", content="", session_id=session_id))

        elif t == "text":
            text = event.get("part", {}).get("text", "")
            events.append(StreamEvent(type="text", content=text))

        elif t == "tool_use":
            tool_name = event.get("part", {}).get("tool", "")
            logger.info("Tool call: %s", tool_name)
            events.append(StreamEvent(type="tool_use", content=tool_name))

        elif t == "step_finish":
            reason = event.get("part", {}).get("reason", "")
            if reason == "stop":
                cost = event.get("part", {}).get("cost", 0) or 0
                logger.info("Result: ok (cost=$%.4f)", cost)
                events.append(StreamEvent(
                    type="result",
                    content="ok",
                    cost=cost,
                    session_id=session_id,
                ))

        elif t == "error":
            message = event.get("error", {}).get("data", {}).get("message", "Unknown error")
            logger.warning("Error: %s", message)
            events.append(StreamEvent(type="error", content=message))

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
        return Path(os.path.expanduser("~/.config/opencode/opencode.json"))

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
        config.setdefault("mcp", {})

        if mcp_server.type == MCPServerType.STDIO:
            entry = {
                "type": "local",
                "command": [mcp_server.command, *mcp_server.args],
                "environment": dict(mcp_server.env),
                "enabled": True,
            }
        else:
            entry = {
                "type": "remote",
                "url": mcp_server.url,
                "headers": dict(mcp_server.headers),
                "enabled": True,
            }

        config["mcp"][mcp_server.name] = entry
        self._write_config(config)

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        config = self._read_config()
        servers = config.get("mcp", {})
        if mcp_server_name not in servers:
            warnings.warn(
                f"MCP server '{mcp_server_name}' not found in OpenCode config",
                UserWarning,
                stacklevel=2,
            )
            return

        del servers[mcp_server_name]
        config["mcp"] = servers
        self._write_config(config)

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        config = self._read_config()
        servers = config.get("mcp", {})
        result: list[MCPServerSpec] = []

        for name, entry in servers.items():
            if not isinstance(entry, dict):
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': expected object, got {type(entry).__name__}",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            try:
                if entry.get("type") == "local":
                    command_array = entry.get("command", [])
                    command = command_array[0] if command_array else None
                    args = list(command_array[1:])
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.STDIO,
                        command=command,
                        args=args,
                        env=dict(entry.get("environment", {})),
                    ))
                elif entry.get("type") == "remote":
                    result.append(MCPServerSpec(
                        name=name,
                        type=MCPServerType.HTTP,
                        url=entry.get("url"),
                        headers=dict(entry.get("headers", {})),
                    ))
            except ValueError as e:
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': {e}",
                    UserWarning,
                    stacklevel=2,
                )

        return result
