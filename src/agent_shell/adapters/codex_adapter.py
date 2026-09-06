import asyncio
import codecs
import json
import logging
import os
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.adapters.health import run_health_probe
from agent_shell.adapters.model_discovery import decode_model_output, run_model_command
from agent_shell.adapters.process_failure import process_failure_event
from agent_shell.adapters.response import collect_response
from agent_shell.adapters.stderr_format import format_stderr
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

logger = logging.getLogger("agent_shell.codex_adapter")


class CodexAdapter:
    def prepare_interactive(
        self, directory: Path, *, prompt: str | None, model: str | None,
        effort: str | None, session_id: str | None, allowed_tools: list[str] | None = None,
    ):
        if allowed_tools is not None:
            raise NotImplementedError(
                "Interactive allowed_tools is not implemented for this harness"
            )

        from agent_shell.interactive import InteractiveLaunch, event_writer_command

        command = ["codex"]
        if session_id:
            command += ["resume", session_id]
        # A per-invocation override; the user's config file is never changed.
        command += ["-c", "notify=" + json.dumps(event_writer_command(directory))]
        if model:
            command += ["--model", model]
        if effort:
            command += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        if prompt is not None:
            command += ["--", prompt]
        return InteractiveLaunch(
            command, self.parse_interactive_event,
            frozenset({"text", "session_id", "turn_complete"}),
        )

    def parse_interactive_event(self, event: dict) -> list[StreamEvent]:
        """Normalize Codex's after-turn notification, without guessing usage or failures."""
        if event.get("type") != "agent-turn-complete":
            return []
        session_id = event.get("thread-id")
        events = [StreamEvent(type="system", content="", session_id=session_id)]
        text = event.get("last-assistant-message")
        if text:
            events.append(StreamEvent(type="text", content=text, session_id=session_id))
        events.append(StreamEvent(type="result", content="ok", session_id=session_id))
        return events

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

        # Codex has no name-based deny; it can only disable web search via a config
        # override. Everything else is warn-and-ignore.
        deny_web_search = bool(disallowed_tools) and "web_search" in disallowed_tools
        if disallowed_tools:
            unsupported = sorted(set(disallowed_tools) - {"web_search"})
            if unsupported:
                # Warn EVERY call (not warn-once like include_thinking/allowed_tools above):
                # a silently dropped deny is a security hole, and a reused adapter instance
                # may request a different unenforceable deny on a later call.
                warnings.warn(
                    f"Codex can only deny web_search; ignoring {unsupported}",
                    UserWarning,
                    stacklevel=2,
                )

        if deny_web_search and effort == "minimal":
            # Codex IGNORES web_search="disabled" under model_reasoning_effort="minimal"
            # (openai/codex#5002), so the only Codex-enforceable deny silently fails OPEN at this
            # effort. Warn EVERY call (same security rationale as the unsupported-deny warning
            # above) rather than emit a no-op deny silently — a caller must never believe the
            # network is blocked when it is not. The flag is still passed (harmless at other
            # efforts on a reused instance); the warning carries the truth.
            warnings.warn(
                'Codex ignores web_search="disabled" under model_reasoning_effort="minimal" '
                "(openai/codex#5002); the web_search deny will NOT be enforced this call",
                UserWarning,
                stacklevel=2,
            )

        cmd = self._build_command(
            prompt=prompt,
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            session_id=session_id,
            deny_web_search=deny_web_search,
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
                            for event in self._parse_event(raw):
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
                            for event in self._parse_event(raw):
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
            #
            # On abandonment this is NOT synchronous with the consumer's `break`: CPython
            # schedules the generator's aclose() as a separate async_generator_athrow task, so
            # the child is still alive and still registered until a later turn of the loop, and
            # if the loop is torn down first (asyncio.run cancelling pending tasks) it never
            # runs at all. That last case is what the atexit net in process_cleanup covers.
            await release_process(process, self._active_processes, stderr_task,
                                  child_exited=child_exited)

    def _build_command(
            self,
            prompt: str,
            model: str | None,
            effort: str | None,
            auto_approve: bool,
            session_id: str | None,
            deny_web_search: bool = False,
    ) -> list[str]:
        # `web_search` is a TOML string config (disabled/cached/live), so the value must be
        # quoted like model_reasoning_effort. Verified accepted AND enforced on codex-cli
        # 0.133.0 (incl. under --dangerously-bypass-approvals-and-sandbox). This single key is
        # the entire Codex deny capability, so it is load-bearing and version-fragile: upstream
        # is moving toward `web_search_mode`, and a future Codex could rename/reject the
        # top-level key and silently turn this deny into a no-op. The e2e guard in
        # tests/e2e/test_codex_e2e.py is what catches that. Separately, Codex ignores
        # web_search="disabled" under model_reasoning_effort="minimal" (openai/codex#5002).
        if session_id:
            cmd = ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
            if model:
                cmd.extend(["--model", model])
            if effort:
                cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
            if deny_web_search:
                cmd.extend(["-c", 'web_search="disabled"'])
            cmd.extend([session_id, prompt])
            return cmd

        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write"]
        if auto_approve:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        if deny_web_search:
            cmd.extend(["-c", 'web_search="disabled"'])
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

        elif t == "turn.failed":
            # Codex surfaces the real failure reason here on stdout (bad model, usage limit,
            # invalid request). The process also exits non-zero with only "Reading additional
            # input from stdin..." on stderr, so without this the reason would be lost.
            message = (event.get("error") or {}).get("message") or "turn failed"
            logger.warning("Turn failed: %s", message)
            events.append(StreamEvent(type="error", content=message))

        elif t == "turn.completed":
            # One turn.completed per `codex exec`, so its usage is the whole-run total. Codex
            # mirrors the OpenAI Responses API where usage.output_tokens already INCLUDES
            # reasoning tokens — which is what we want: this is a cost measure and reasoning is
            # billed at the output rate. So report output_tokens raw, no subtraction.
            # `or {}` tolerates a null usage object; `or 0` a null token field.
            output_tokens = (event.get("usage") or {}).get("output_tokens", 0) or 0
            events.append(StreamEvent(
                type="result", content="ok", cost=0.0, duration=0.0,
                output_tokens=output_tokens,
            ))

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
            *, effort: str | None = None,
    ) -> HealthCheckResult:
        return await run_health_probe(self, cwd, model=model, timeout=timeout, effort=effort)

    async def list_models(
            self,
            cwd: str,
            timeout: float = 30.0,
    ) -> list[str]:
        cmd = ["codex", "debug", "models"]
        returncode, stdout, stderr = await run_model_command(
            cmd,
            cwd,
            timeout,
        )
        if returncode != 0:
            message = format_stderr(stderr) or f"exit code {returncode}"
            raise RuntimeError(f"`codex debug models` failed: {message}")

        output = decode_model_output(stdout, "`codex debug models`")
        try:
            catalog = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "`codex debug models` returned invalid JSON"
            ) from error
        models = catalog.get("models") if isinstance(catalog, dict) else None
        if not isinstance(models, list):
            raise RuntimeError("`codex debug models` returned no model catalog")

        visible_models: list[str] = []
        for model in models:
            if not isinstance(model, dict):
                raise RuntimeError("`codex debug models` returned an invalid model entry")
            if model.get("visibility") != "list":
                continue
            slug = model.get("slug")
            if not isinstance(slug, str) or not slug:
                raise RuntimeError("`codex debug models` returned an invalid model entry")
            visible_models.append(slug)
        return visible_models

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
