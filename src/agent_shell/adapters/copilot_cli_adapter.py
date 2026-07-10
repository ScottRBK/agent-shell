import asyncio
import codecs
import json
import logging
import os
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType, HealthCheckResult
from agent_shell.process_cleanup import register_process_group, unregister_process_group, kill_process_group
from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.stderr_format import format_stderr
from agent_shell.adapters.tool_denial import resolve_disallowed_tools

logger = logging.getLogger("agent_shell.copilot_cli_adapter")

# Canonical deny-name -> Copilot CLI `--deny-tool` permission names.
# Only `shell` and `write` are confirmed permission names for the CLI's tool flags
# (`copilot --help`: `--allow-tool='shell(git:*)'`, `--allow-tool='write'`). Copilot CLI
# has no `web_search`/`web_fetch` tools (its web tool is `fetch`), and the SDK tool
# vocabulary (`bash`/`edit`/`view`) differs from the CLI flag vocabulary — so guessing
# native names for read/web access risks a SILENT no-op deny, because Copilot ignores
# unrecognized `--deny-tool` names. `read`/`web_search`/`web_fetch` are therefore left
# unmapped so the adapter warns loudly (fail-loud) rather than pretending to deny; a caller
# who knows their build's exact tool name can still pass it verbatim (e.g. ["view"]).
_DISALLOWED_TOOL_MAP = {
    "bash": ["shell"],
    "edit": ["write"],
}


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
            "copilot", "-p", prompt,
            "--output-format", "json",
            "--silent",
        ]

        if auto_approve:
            cmd.append("--allow-all-tools")

        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allow-tool", tool])

        # Deny-list. Copilot docs: "Deny rules always take precedence", so --deny-tool
        # holds even alongside --allow-all-tools. MCP tools (Server(tool) syntax) pass
        # through verbatim as unknown names.
        native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
        if unsupported:
            warnings.warn(
                f"Copilot CLI cannot deny {unsupported}; ignoring",
                UserWarning,
                stacklevel=2,
            )
        for tool in native:
            cmd.extend(["--deny-tool", tool])

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

        # Drain stderr concurrently with stdout. Reading it only after the stdout loop can
        # deadlock: a child that fills its stderr pipe buffer (~64KB) mid-run blocks on that
        # write, never closes stdout, and our stdout read() then waits forever.
        stderr_task = asyncio.ensure_future(process.stderr.read())

        # Incremental decoder so a multibyte char split across two reads is stitched back
        # together instead of raising UnicodeDecodeError; "replace" keeps a truly truncated
        # tail from aborting the run.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        # Per-run accumulator (local to this stream() call, never instance state, so counts
        # never leak between runs on a reused adapter). Copilot reports output tokens per
        # assistant.message; we sum them and stamp the total on the result event.
        run_output_tokens = 0
        try:
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    buffer += decoder.decode(b"", final=True)
                    if buffer.strip():
                        try:
                            raw = json.loads(buffer)
                            logger.debug("Raw event: %s", raw)
                            run_output_tokens += self._message_output_tokens(raw)
                            for event in self._parse_event(
                                event=raw,
                                include_thinking=include_thinking,
                                run_output_tokens=run_output_tokens,
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
                            run_output_tokens += self._message_output_tokens(raw)
                            for event in self._parse_event(
                                event=raw,
                                include_thinking=include_thinking,
                                run_output_tokens=run_output_tokens,
                            ):
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

    def _message_output_tokens(self, raw: dict) -> int:
        """Per-message output-token count from an assistant.message event (0 otherwise).

        Copilot reports output tokens per assistant.message (not cumulative) and the result
        event carries none, so stream() sums these across the run and stamps the total on the
        result event.
        """
        if raw.get("type") != "assistant.message":
            return 0
        return (raw.get("data") or {}).get("outputTokens", 0) or 0

    def _parse_event(
        self, event: dict, include_thinking: bool, run_output_tokens: int = 0
    ) -> list[StreamEvent]:
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

        elif t == "assistant.message":
            # Text is surfaced here on the full message `content`, not from
            # assistant.message_delta's per-token deltaContent. deltaContent is meant to be
            # concatenated directly (no separator); execute()'s "\n".join over "text" events
            # would otherwise explode the response into one token per line (issue #6).
            data = event.get("data", {})
            content = data.get("content", "")
            if content:
                events.append(StreamEvent(type="text", content=content))
            tool_requests = data.get("toolRequests", [])
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
                output_tokens=run_output_tokens,
            ))

        return events

    async def cancel(self) -> None:
        for process in self._active_processes:
            kill_process_group(process.pid)
        self._active_processes.clear()

    async def health_check(
            self,
            cwd: str,
            model: str | None = None,
            timeout: float = 60.0,
    ) -> HealthCheckResult:
        return await run_health_probe(self, cwd, model=model, timeout=timeout)

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
            if not isinstance(entry, dict):
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': expected object, got {type(entry).__name__}",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            try:
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
            except ValueError as e:
                warnings.warn(
                    f"Skipping malformed MCP entry '{name}': {e}",
                    UserWarning,
                    stacklevel=2,
                )

        return result
