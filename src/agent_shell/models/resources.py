"""Explicit, per-session resource choices; native formats remain harness-specific."""

from dataclasses import dataclass, field

from agent_shell.models.agent import MCPServerSpec, PackageSpec


@dataclass(frozen=True)
class SessionResources:
    """Omitted lists add nothing. MCP None inherits; an empty list excludes all native MCP.

    Inheritance defaults preserve native discovery. Unsupported exclusions raise before launch.
    Local paths are relative to the session cwd. Selection is fixed when the session starts.
    MCP selection is identified by server names; connection configuration may change on resume.
    """

    skills: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    packages: list[PackageSpec] = field(default_factory=list)
    mcp_servers: list[MCPServerSpec] | None = None
    inherit_personal_resources: bool = True
    inherit_project_resources: bool = True
    include_instruction_files: bool = True
    include_home_system_prompt: bool = True
