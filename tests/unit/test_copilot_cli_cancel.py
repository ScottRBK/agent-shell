from unittest.mock import AsyncMock

from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter


class TestCancel:
    async def test_cancels_active_run_handles_and_clears_list(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        mock_process = AsyncMock()
        mock_process.pid = 12345
        adapter._active_processes = [mock_process]

        # Act
        await adapter.cancel()

        # Assert
        mock_process.cancel.assert_awaited_once_with()
        assert len(adapter._active_processes) == 0
