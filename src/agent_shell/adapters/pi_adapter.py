import asyncio
import codecs
import json
import logging
import os
import warnings
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, HealthCheckResult
from agent_shell.process_cleanup import register_process_group, unregister_process_group
from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.stderr_format import format_stderr
from agent_shell.adapters.tool_denial import resolve_disallowed_tools

logger = logging.getLogger("agent_shell.pi_adapter")

# Canonical deny-name -> Pi native tool names. Pi's built-in tools are
# read/bash/edit/write; `edit` fans out to edit+write. web_search/web_fetch are
# intentionally absent: Pi ships no built-in web tool, so those denies are
# unenforceable and warn (a deny that silently no-ops is a security hole).
# Non-canonical names pass through verbatim to --exclude-tools, letting a caller
# deny a specifically-named extension tool.
_DISALLOWED_TOOL_MAP = {
    "bash": ["bash"],
    "edit": ["edit", "write"],
    "read": ["read"],
}


class PiAdapter:
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
        native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
        if unsupported:
            # Warn EVERY call (not warn-once): a silently dropped deny is a security hole,
            # and a reused adapter instance may request a different unenforceable deny later.
            warnings.warn(
                f"Pi has no built-in tool to deny for {unsupported}; ignoring",
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

    def _build_command(
            self,
            prompt: str,
            model: str | None,
            effort: str | None,
            auto_approve: bool,
            session_id: str | None,
            allowed_tools: list[str] | None = None,
            disallowed_native: list[str] | None = None,
    ) -> list[str]:
        cmd = ["pi", "--mode", "json", "--print"]

        # A trust decision MUST be passed explicitly. With neither --approve nor
        # --no-approve, `pi -p` blocks on an interactive "trust project?" prompt and
        # never returns. auto_approve trusts (and runs) project-local files; the
        # negative ignores them but stays non-interactive.
        cmd.append("--approve" if auto_approve else "--no-approve")

        if model:
            cmd.extend(["--model", model])

        # Pi has no separate effort flag; --thinking IS its reasoning-effort knob and its
        # levels (off/minimal/low/medium/high/xhigh) match the effort vocabulary.
        if effort:
            cmd.extend(["--thinking", effort])

        if allowed_tools:
            cmd.extend(["--tools", ",".join(allowed_tools)])

        if disallowed_native:
            cmd.extend(["--exclude-tools", ",".join(disallowed_native)])

        if session_id:
            cmd.extend(["--session-id", session_id])

        # Prompt is a positional message; keep it LAST.
        cmd.append(prompt)
        return cmd

    def _parse_event(self, event: dict, include_thinking: bool) -> list[StreamEvent]:
        t = event.get("type", "")
        events: list[StreamEvent] = []

        if t == "session":
            session_id = event.get("id")
            if session_id:
                logger.info("Session: %s", session_id)
                events.append(StreamEvent(type="system", content="", session_id=session_id))

        elif t == "message_update":
            ame = event.get("assistantMessageEvent") or {}
            sub = ame.get("type")
            # Text and thinking are surfaced on their `_end` event (full block). Streaming
            # the per-token deltas instead would corrupt execute()'s newline-join of text.
            if sub == "text_end":
                content = ame.get("content") or ""
                if content:
                    events.append(StreamEvent(type="text", content=content))
            elif sub == "thinking_end" and include_thinking:
                content = ame.get("content") or ""
                if content:
                    events.append(StreamEvent(type="thinking", content=content))

        elif t == "tool_execution_start":
            tool_name = event.get("toolName", "")
            if tool_name:
                logger.info("Tool call: %s", tool_name)
                events.append(StreamEvent(type="tool_use", content=tool_name))

        elif t == "agent_end":
            # One agent_end per run, carrying every message. Sum usage over the assistant
            # turns: output is a cost measure (reasoning-inclusive) and cost.total is real
            # for paid providers (0 on local). pi exits 0 even on a model error, so failure
            # is detected here via stopReason, not from the process return code.
            output_tokens = 0
            cost = 0.0
            is_error = False
            for message in event.get("messages") or []:
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                output_tokens += usage.get("output", 0) or 0
                cost += (usage.get("cost") or {}).get("total", 0) or 0
                if message.get("stopReason") == "error":
                    is_error = True
            status = "error" if is_error else "ok"
            logger.info("Result: %s (cost=$%.4f, output_tokens=%d)", status, cost, output_tokens)
            events.append(StreamEvent(
                type="result", content=status, cost=cost, duration=0.0,
                output_tokens=output_tokens,
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
        # Pi manages capability through `pi install` extensions and a settings file with no
        # documented MCP subcommand; the mechanism needs investigation before wiring it up.
        raise NotImplementedError("add_mcp_server is not yet implemented for Pi")

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        raise NotImplementedError("remove_mcp_server is not yet implemented for Pi")

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        raise NotImplementedError("list_mcp_servers is not yet implemented for Pi")
