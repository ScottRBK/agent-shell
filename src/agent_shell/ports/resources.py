"""Adapter-owned preparation of invocation-scoped resources."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_shell.models.resources import SessionResources


@dataclass
class ResourceLaunch:
    arguments: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


class ResourceAdapter(Protocol):
    async def prepare_resources(
        self, resources: SessionResources, cwd: str, directory: Path,
        *, auto_approve: bool | None = None,
    ) -> ResourceLaunch:
        """Validate before installing; keep temporary files within directory.

        All adapters expose this async signature, including implementations that only
        construct arguments. Pi may await package installation. Implementations may not need
        every input: cwd resolves project resources; directory holds temporary configuration;
        auto_approve is the headless trust choice, or None for an interactive trust decision.
        """
        ...
