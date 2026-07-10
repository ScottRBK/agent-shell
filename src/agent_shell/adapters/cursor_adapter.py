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

logger = logging.getLogger("agent_shell.cursor_adapter")


class CursorAdapter:
    def __init__(self):
        self._active_processes = []
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
        # Cursor's `mcp` subcommands are login/list/list-tools/enable/disable ONLY — there is
        # no add/remove. Servers are declared in .cursor/mcp.json, and `mcp list` reports only
        # `name: status` (no transport/command/url), so an MCPServerSpec cannot be faithfully
        # round-tripped either. Fail loud rather than silently no-op.
        raise NotImplementedError("add_mcp_server is not supported for Cursor")

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        raise NotImplementedError("remove_mcp_server is not supported for Cursor")

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        raise NotImplementedError("list_mcp_servers is not supported for Cursor")
