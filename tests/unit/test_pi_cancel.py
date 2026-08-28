from unittest.mock import AsyncMock

from agent_shell.adapters.pi_adapter import PiAdapter


class TestCancel:
    async def test_cancels_active_run_handles_and_clears_list(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = AsyncMock()
        mock_process.pid = 12345
        adapter._active_processes = [mock_process]

        # Act
        await adapter.cancel()

        # Assert
        mock_process.cancel.assert_awaited_once_with()
        assert len(adapter._active_processes) == 0
