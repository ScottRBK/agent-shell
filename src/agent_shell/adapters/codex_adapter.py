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
