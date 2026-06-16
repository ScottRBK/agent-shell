import asyncio
import json
import logging
import os
import warnings
from pathlib import Path
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent, MCPServerSpec, MCPServerType
from agent_shell.process_cleanup import register_process_group, unregister_process_group
from agent_shell.adapters.tool_denial import resolve_disallowed_tools

logger = logging.getLogger("agent_shell.opencode_adapter")

# Canonical deny-name -> OpenCode permission keys. OpenCode collapses write/edit/patch
# into one `edit` permission, so canonical `edit` maps to it directly.
_DISALLOWED_TOOL_MAP = {
    "bash": ["bash"],
    "edit": ["edit"],
    "read": ["read"],
    "web_search": ["websearch"],
    "web_fetch": ["webfetch"],
}


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
            disallowed_tools: list[str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        cmd = ["opencode", "run", "--format", "json"]

        if auto_approve:
            # opencode run auto-REJECTS permission prompts in non-interactive
            # mode; without this flag a single ask (e.g. reading a file outside
            # the project directory) silently aborts the agent loop. This is safe
            # to combine with the OPENCODE_PERMISSION denies below: a `deny` rule
            # raises DeniedError before any `permission.asked` event is published,
            # so the flag (which only auto-approves `ask` events) never sees a
            # denied tool. Verified — object-form deny holds under this flag on
            # opencode 1.14.41 (permission/index.ts deny short-circuit); the e2e
            # guard re-checks it on upgrade.
            cmd.append("--dangerously-skip-permissions")

        if model:
            cmd.extend(["-m", model])

        if session_id:
            cmd.extend(["-s", session_id])

        cmd.append(prompt)

        # One env dict (a single env= kwarg) carrying two concerns:
        #  - PWD pinned to cwd: opencode resolves its project directory — and with
        #    it the permission boundary — from $PWD, so a stale inherited PWD (the
        #    launcher's dir, not `cwd`) would misplace the project root.
        #  - OPENCODE_PERMISSION: the disallowed_tools denies, scoped to this
        #    subprocess so we never touch the user's global config.
        env = self._build_subprocess_env(cwd, disallowed_tools)

        logger.debug("Command: %s", cmd)
        logger.info("Process started (cwd=%s)", os.path.abspath(cwd))

        process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.abspath(cwd),
                preexec_fn=os.setsid,
                env=env,
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

    def _build_subprocess_env(self, cwd: str, disallowed_tools: list[str] | None) -> dict[str, str]:
        """Build the child env: pin PWD to cwd and layer OPENCODE_PERMISSION denies.

        Always returns a dict (never None): PWD must be pinned on every run because opencode
        resolves its project directory — and with it the permission boundary — from $PWD, so a
        stale inherited PWD would misplace the project root.

        OPENCODE_PERMISSION handling (our denies always win on conflict):
          * caller `disallowed_tools` -> per-tool "deny", merged on top of any inherited policy;
          * an inherited bare-string "deny" is promoted to the {"*": "deny"} wildcard opencode
            actually enforces — even when the caller passes NO deny-list — because re-forwarding
            the bare string is a silent no-op under --dangerously-skip-permissions and would fail
            OPEN on the user's global deny-all intent;
          * an inherited object policy flows through `base` untouched when we have nothing to add;
          * an unparseable / non-object inherited value is dropped (fail-closed) with a warning,
            but only when we are actually applying denies on top of it.
        """
        base = {**os.environ, "PWD": os.path.abspath(cwd)}

        native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
        if unsupported:
            warnings.warn(
                f"OpenCode cannot deny {unsupported}; ignoring",
                UserWarning,
                stacklevel=2,
            )

        existing, promoted = self._inherited_permission(warn=bool(native))

        # Leave the parent env untouched only when we have nothing of our own to add (no caller
        # denies) AND nothing we were forced to rewrite (no bare-"deny" promotion). Otherwise an
        # inherited object simply rides through `base`, and we inject no spurious permission key.
        if not native and not promoted:
            return base

        merged = {**existing, **{tool: "deny" for tool in native}}
        return {**base, "OPENCODE_PERMISSION": json.dumps(merged)}

    def _inherited_permission(self, warn: bool) -> tuple[dict, bool]:
        """Parse an inherited OPENCODE_PERMISSION into object form for merging.

        Returns (existing, promoted):
          existing  – the inherited per-tool map to merge our denies under ({} when absent or
                      unmergeable).
          promoted  – True only when a bare-string "deny" was rewritten to {"*": "deny"}. opencode's
                      env-var path runs the raw JSON.parse result through remeda mergeDeep, which
                      DROPS a primitive string, so re-emitting bare "deny" is a silent no-op
                      (verified on opencode 1.14.41). The rewrite must therefore be emitted even
                      with no caller denies — otherwise a global deny-all fails OPEN.

        `warn` gates the fail-loud warnings: we only surface an unmergeable inherited value when we
        are actually applying denies on top of it (a no-deny passthrough leaves it for opencode).
        """
        raw = os.environ.get("OPENCODE_PERMISSION")
        if not raw:
            return {}, False

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            if warn:
                warnings.warn(
                    "Existing OPENCODE_PERMISSION is invalid JSON; "
                    "applying disallowed_tools denies on top of an empty base",
                    UserWarning,
                    stacklevel=3,
                )
            return {}, False

        if isinstance(parsed, dict):
            return parsed, False
        if parsed == "deny":
            return {"*": "deny"}, True
        if warn:
            # A permissive scalar ("allow"/"ask") or other non-object value: not a per-tool map.
            # Apply our denies on an empty base — at least as restrictive as the inherited value for
            # the tools we care about — and surface that we couldn't merge granularly.
            warnings.warn(
                "Existing OPENCODE_PERMISSION is not a JSON object; "
                "applying disallowed_tools denies on top of an empty base",
                UserWarning,
                stacklevel=3,
            )
        return {}, False

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
