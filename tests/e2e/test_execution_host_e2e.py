"""Real-CLI smoke coverage for the opt-in execution boundary.

Local-only: this uses the authenticated Claude Code CLI and incurs a small Haiku call.
"""

import sys

import pytest

from agent_shell.execution import (
    IsolationUnavailableError,
    LinuxPidNamespaceIsolation,
    NativeExecutionHost,
)
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell

pytestmark = pytest.mark.e2e


async def test_real_claude_health_check_completes_inside_pid_isolation():
    # Arrange
    policy = LinuxPidNamespaceIsolation()
    host = NativeExecutionHost()
    try:
        probe = await host.launch(
            [sys.executable, "-c", "pass"],
            cwd="/tmp",
            isolation_policy=policy,
        )
    except IsolationUnavailableError as error:
        pytest.skip(str(error))
    await probe.communicate()
    probe.release()

    shell = AgentShell(
        agent_type=AgentType.CLAUDE_CODE,
        isolation_policy=policy,
    )

    # Act
    result = await shell.health_check(cwd="/tmp", model="haiku")

    # Assert
    assert result.healthy is True, result.exception
    assert result.exception is None
