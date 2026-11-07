"""Provider-independent resource snapshots and temporary-file lifetime."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from agent_shell.models.agent import MCPServerSpec, PackageSpec
from agent_shell.models.resources import SessionResources
from agent_shell.ports.resources import ResourceAdapter


def snapshot_resources(resources: SessionResources, cwd: str) -> SessionResources:
    if not isinstance(resources, SessionResources):
        raise TypeError("resources must be SessionResources")
    resources = deepcopy(resources)
    for name in ("inherit_personal_resources", "inherit_project_resources",
                 "include_instruction_files", "include_home_system_prompt"):
        if type(getattr(resources, name)) is not bool:
            raise ValueError(f"{name} must be a boolean")
    paths = {name: _resource_paths(getattr(resources, name), name, cwd)
             for name in ("skills", "plugins", "extensions")}
    if not isinstance(resources.packages, (list, tuple)) or any(
        not isinstance(item, PackageSpec) for item in resources.packages
    ):
        raise ValueError("packages must contain PackageSpec objects")
    _validate_mcp_selection(resources.mcp_servers)
    return replace(resources, **paths)


def _resource_paths(values: list[str], name: str, cwd: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list of paths")
    resolved = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{name} must contain nonempty paths")
        path = Path(os.path.abspath(Path(cwd) / Path(value).expanduser()))
        if not path.exists():
            raise ValueError(f"{name} path does not exist: {path}")
        resolved.append(str(path))
    return resolved


def _validate_mcp_selection(servers: list[MCPServerSpec] | None) -> None:
    if servers is None:
        return
    if not isinstance(servers, (list, tuple)) or any(
        not isinstance(item, MCPServerSpec) for item in servers
    ):
        raise ValueError("mcp_servers must contain MCPServerSpec objects")
    names = [server.name for server in servers]
    if any(not name or "\x00" in name for name in names) or len(set(names)) != len(names):
        raise ValueError("MCP server names must be nonempty and unique")


@asynccontextmanager
async def resource_session(
    adapter: ResourceAdapter, cwd: str, resources: SessionResources | None, *, auto_approve: bool,
):
    if resources is None:
        yield None
        return
    resources = snapshot_resources(resources, cwd)
    with TemporaryDirectory(prefix="agentshell-resources-") as name:
        yield await adapter.prepare_resources(resources, cwd, Path(name), auto_approve=auto_approve)


def require_resource_support(
    resources: SessionResources, supported: frozenset[str], harness: str,
) -> None:
    """Check a declared subset before an adapter performs any preparation side effects."""
    requested = {name for name in ("skills", "plugins", "extensions", "packages")
                 if getattr(resources, name)}
    requested.update(name for name in (
        "inherit_personal_resources", "inherit_project_resources",
        "include_instruction_files", "include_home_system_prompt",
    ) if not getattr(resources, name))
    if resources.mcp_servers is not None:
        requested.add("mcp_servers")
    unsupported = requested - supported
    if unsupported:
        raise NotImplementedError(f"{harness} session resources do not support: "
                                  + ", ".join(sorted(unsupported)))


class ResourceSelection:
    """Persist only a digest, so resume cannot silently change a session's resource choices."""

    def __init__(
        self, agent: str, cwd: str, resources: SessionResources | None, session_id: str | None,
    ):
        self.resources = snapshot_resources(resources, cwd) if resources is not None else None
        self._agent = agent
        self._recorded: set[str] = set()
        if self.resources is None and not session_id:
            # A fresh unselected run has nothing to persist or check on disk.
            return
        selection = asdict(self.resources) if self.resources is not None else None
        if selection is not None and self.resources.mcp_servers is not None:
            # A server's name identifies the selection; its connection configuration may change.
            selection["mcp_servers"] = sorted(server.name for server in self.resources.mcp_servers)
        self._digest = hashlib.sha256(json.dumps(
            {"cwd": os.path.abspath(cwd), "resources": selection},
            sort_keys=True,
        ).encode()).hexdigest()
        state = os.environ.get("XDG_STATE_HOME")
        try:
            base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
        except RuntimeError as error:
            raise ValueError(
                "Cannot locate persistent session resource state. "
                "Set XDG_STATE_HOME to a writable directory."
            ) from error
        self._root = base.absolute() / "agent-shell" / "resources"
        if self.resources is not None:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Fail before installation/inference if we cannot retain the selection for resume.
            with NamedTemporaryFile(dir=self._root) as probe:
                probe.write(self._digest.encode())
                probe.flush()
        if session_id:
            self._check_resume(session_id)

    def _check_resume(self, session_id: str) -> None:
        try:
            previous = self._path(session_id).read_text()
        except FileNotFoundError:
            if self.resources is not None:
                raise ValueError(
                    "No recorded resource selection for this session; start a new one"
                )
        else:
            if previous != self._digest:
                raise ValueError(
                    "Resume requires the original resource selection and working directory"
                )

    def _path(self, session_id: str) -> Path:
        # Locate known sessions before checking cwd, so a changed path cannot hide their record.
        key = hashlib.sha256(json.dumps([self._agent, session_id]).encode()).hexdigest()
        return self._root / f"{key}.sha256"

    def record(self, session_id: str | None) -> None:
        if self.resources is None or not session_id or session_id in self._recorded:
            return
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(session_id)
        # Link an already-written file atomically. An existing selection must never be replaced.
        with NamedTemporaryFile(dir=self._root) as output:
            output.write(self._digest.encode())
            output.flush()
            try:
                os.link(output.name, path)
            except FileExistsError:
                if path.read_text() != self._digest:
                    raise ValueError("Session already has a different resource selection")
        self._recorded.add(session_id)
