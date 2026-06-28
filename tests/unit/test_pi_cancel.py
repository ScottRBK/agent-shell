from unittest.mock import patch, AsyncMock, MagicMock

from agent_shell.adapters.pi_adapter import PiAdapter


class TestCancel:
    async def test_kills_active_processes_and_clears_list(self):
        # Arrange
        adapter = PiAdapter()
        mock_process = AsyncMock()
        mock_process.pid = 12345
        adapter._active_processes = [mock_process]

        # Act
        mock_unregister = MagicMock()
        with patch("os.getpgid", return_value=12345) as mock_getpgid, \
             patch("os.killpg") as mock_killpg, \
             patch("agent_shell.adapters.pi_adapter.unregister_process_group", mock_unregister):
            await adapter.cancel()

        # Assert
        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(12345, 9)
        assert len(adapter._active_processes) == 0
        mock_unregister.assert_called_once_with(12345)

    async def test_tolerates_already_dead_process(self):
        # Arrange — a process that already exited raises ProcessLookupError; cancel swallows it.
        adapter = PiAdapter()
        mock_process = AsyncMock()
        mock_process.pid = 999
        adapter._active_processes = [mock_process]

        # Act
        with patch("os.getpgid", side_effect=ProcessLookupError), \
             patch("os.killpg"):
            await adapter.cancel()

        # Assert
        assert adapter._active_processes == []
