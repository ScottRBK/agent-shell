from unittest.mock import patch, AsyncMock

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter


class TestCancel:
    async def test_kills_active_processes_and_clears_list(self):
        # Arrange
        adapter = OpenCodeAdapter()
        mock_process = AsyncMock()
        mock_process.pid = 12345
        adapter._active_processes = [mock_process]

        # Act
        with patch("os.getpgid", return_value=12345) as mock_getpgid, \
             patch("os.killpg") as mock_killpg:
            await adapter.cancel()

        # Assert
        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(12345, 9)
        assert len(adapter._active_processes) == 0
