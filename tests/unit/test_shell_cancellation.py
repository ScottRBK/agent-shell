"""Execution and streaming stop the external child when interrupted."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType


@pytest.mark.parametrize("mode", ["execute", "stream"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
async def test_interruption_stops_the_child(mode, interruption):
    # Arrange: interrupt the external stdout boundary after launch.
    run = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=interruption)),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
        returncode=None,
        cancel=AsyncMock(),
    )
    host = SimpleNamespace(launch=AsyncMock(return_value=run))
    shell = AgentShell(AgentType.CLAUDE_CODE, execution_host=host)

    # Act / Assert
    with pytest.raises(interruption):
        if mode == "execute":
            await shell.execute(cwd="/tmp", prompt="test")
        else:
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass
    run.cancel.assert_awaited_once()
