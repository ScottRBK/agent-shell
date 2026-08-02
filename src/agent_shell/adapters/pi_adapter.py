import asyncio
import codecs
import json
import logging
import os
import re
import warnings
from typing import AsyncIterator

from agent_shell.models.agent import (
    AgentResponse,
    HealthCheckResult,
    MCPServerSpec,
    StreamEvent,
)
from agent_shell.process_cleanup import (
    create_grouped_process,
    kill_process_group,
    release_process,
)
from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.model_discovery import decode_model_output, run_model_command
from agent_shell.adapters.response import collect_response
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

# Pi's StopReason union is stop|length|toolUse|error|aborted. Neither an errored nor an
# aborted turn produced a completed answer, so both must report status "error".
_FAILURE_STOP_REASONS = ("error", "aborted")


def _failure_reason(message: dict) -> str:
    """Best available explanation for a failed assistant message.

    Pi carries the real cause in `errorMessage` (e.g. "500 model name=... failed to
    load"). It is optional on pi's type, so when it is missing fall back to the
    stopReason plus the model identity — still far more actionable for a caller than
    the bare "error" they got before.
    """
    reason = message.get("errorMessage")
    if reason:
        return str(reason)
    identity = [f"{key}={message[key]}" for key in ("provider", "model") if message.get(key)]
    stop_reason = str(message.get("stopReason") or "error")
    return f"{stop_reason} ({', '.join(identity)})" if identity else stop_reason


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

        process = await create_grouped_process(
            cmd,
            cwd=os.path.abspath(cwd),
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
            if stderr and process.returncode != 0:
                error_msg = format_stderr(stderr)
                logger.warning("Process exited with code %d: %s", process.returncode, error_msg)
                yield StreamEvent(type="error", content=error_msg)
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
            release_process(process, self._active_processes, stderr_task,
                            child_exited=child_exited)

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
            # agent_end carries `messages` = the agent loop's `newMessages`, i.e. only what
            # THIS loop run produced. Prior turns of a resumed --session-id session live in
            # the loop's context and are never in here, so summing usage over these assistant
            # messages bills the caller for their own run and nothing else. output is a cost
            # measure (reasoning-inclusive) and cost.total is real for paid providers (0 on
            # local).
            #
            # A run can emit SEVERAL agent_end events, one per agent loop: pi auto-retries a
            # retryable fault by default and continues the agent, and auto-compaction does
            # the same. Each is judged on its own, and outcome.py takes the last as the
            # verdict.
            #
            # pi exits 0 even on a model error, so failure is detected here via stopReason,
            # not from the process return code — and only the CURRENT turn's stopReason
            # counts. That is the last assistant message, which is how pi itself reads an
            # agent_end (agent-session.js `_willRetryAfterAgentEnd`, print-mode's text
            # output). Folding the whole list would let a turn the agent already recovered
            # from raise on a run that finished fine.
            output_tokens = 0
            cost = 0.0
            current_turn: dict | None = None
            for message in event.get("messages") or []:
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                output_tokens += usage.get("output", 0) or 0
                cost += (usage.get("cost") or {}).get("total", 0) or 0
                current_turn = message
            failed = bool(current_turn) and \
                current_turn.get("stopReason") in _FAILURE_STOP_REASONS
            error: str | None = _failure_reason(current_turn) if failed else None
            status = "error" if failed else "ok"
            logger.info("Result: %s (cost=$%.4f, output_tokens=%d)", status, cost, output_tokens)
            if error:
                logger.warning("Agent failed: %s", error)
            events.append(StreamEvent(
                type="result", content=status, cost=cost, duration=0.0,
                output_tokens=output_tokens, error=error,
            ))

        return events

    async def cancel(self) -> None:
        for process in self._active_processes:
            kill_process_group(process)
        self._active_processes.clear()

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
        cmd = ["pi", "--no-approve", "--list-models"]
        returncode, stdout, stderr = await run_model_command(
            cmd,
            cwd,
            timeout,
        )
        if returncode != 0:
            message = format_stderr(stderr) or f"exit code {returncode}"
            raise RuntimeError(f"`pi --list-models` failed: {message}")
        if stderr:
            message = format_stderr(stderr).strip()
            raise RuntimeError(f"`pi --list-models` returned warnings: {message}")

        output = decode_model_output(stdout, "`pi --list-models`")
        if output.startswith("No models available"):
            return []

        lines = [line for line in output.splitlines() if line]
        expected_header = [
            "provider", "model", "context", "max-out", "thinking", "images"
        ]
        if not lines or re.split(r"\s{2,}", lines[0].strip()) != expected_header:
            raise RuntimeError("Unexpected `pi --list-models` output")

        models: list[str] = []
        for line in lines[1:]:
            columns = re.split(r"\s{2,}", line.strip())
            if len(columns) != len(expected_header):
                raise RuntimeError("Unexpected `pi --list-models` output")
            models.append(f"{columns[0]}/{columns[1]}")
        return models

    async def add_mcp_server(self, mcp_server: MCPServerSpec) -> None:
        # Pi manages capability through `pi install` extensions and a settings file with no
        # documented MCP subcommand; the mechanism needs investigation before wiring it up.
        raise NotImplementedError("add_mcp_server is not yet implemented for Pi")

    async def remove_mcp_server(self, mcp_server_name: str) -> None:
        raise NotImplementedError("remove_mcp_server is not yet implemented for Pi")

    async def list_mcp_servers(self) -> list[MCPServerSpec]:
        raise NotImplementedError("list_mcp_servers is not yet implemented for Pi")
