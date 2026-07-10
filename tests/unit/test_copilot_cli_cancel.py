from unittest.mock import patch, AsyncMock, MagicMock

from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter


class TestCancel:
    async def test_kills_active_processes_and_clears_list(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        mock_process = AsyncMock()
        mock_process.pid = 12345
        adapter._active_processes = [mock_process]

        # Act
        mock_kill = MagicMock()
        with patch("agent_shell.adapters.copilot_cli_adapter.kill_process_group", mock_kill):
            await adapter.cancel()

        # Assert
        mock_kill.assert_called_once_with(12345)
        assert len(adapter._active_processes) == 0
