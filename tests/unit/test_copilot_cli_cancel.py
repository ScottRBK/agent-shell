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
        mock_unregister = MagicMock()
        with patch("os.getpgid", return_value=12345) as mock_getpgid, \
             patch("os.killpg") as mock_killpg, \
             patch("agent_shell.adapters.copilot_cli_adapter.unregister_process_group", mock_unregister):
            await adapter.cancel()

        # Assert
        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(12345, 9)
        assert len(adapter._active_processes) == 0
        mock_unregister.assert_called_once_with(12345)
