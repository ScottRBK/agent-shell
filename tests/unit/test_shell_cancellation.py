"""Tests that AgentShell calls cancel() on CancelledError, not just KeyboardInterrupt."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType


class TestExecuteCancellation:
    async def test_calls_cancel_on_keyboard_interrupt(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter = AsyncMock()
        shell._adapter.execute = AsyncMock(side_effect=KeyboardInterrupt)

        # Act / Assert
        with pytest.raises(KeyboardInterrupt):
            await shell.execute(cwd="/tmp", prompt="test")

        shell._adapter.cancel.assert_awaited_once()

    async def test_calls_cancel_on_cancelled_error(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter = AsyncMock()
        shell._adapter.execute = AsyncMock(side_effect=asyncio.CancelledError)

        # Act / Assert
        with pytest.raises(asyncio.CancelledError):
            await shell.execute(cwd="/tmp", prompt="test")

        shell._adapter.cancel.assert_awaited_once()


class TestStreamCancellation:
    async def test_calls_cancel_on_keyboard_interrupt(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter = AsyncMock()

        async def _raise_ki(**kwargs):
            raise KeyboardInterrupt
            yield  # noqa: unreachable - makes this an async generator

        shell._adapter.stream = _raise_ki

        # Act / Assert
        with pytest.raises(KeyboardInterrupt):
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        shell._adapter.cancel.assert_awaited_once()

    async def test_calls_cancel_on_cancelled_error(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        shell._adapter = AsyncMock()

        async def _raise_cancel(**kwargs):
            raise asyncio.CancelledError
            yield  # noqa: unreachable - makes this an async generator

        shell._adapter.stream = _raise_cancel

        # Act / Assert
        with pytest.raises(asyncio.CancelledError):
            async for _ in shell.stream(cwd="/tmp", prompt="test"):
                pass

        shell._adapter.cancel.assert_awaited_once()
