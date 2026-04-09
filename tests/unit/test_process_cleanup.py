"""Tests for the process group cleanup registry (safety net for orphaned processes)."""
from unittest.mock import patch, call

from agent_shell.process_cleanup import (
    register_process_group,
    unregister_process_group,
    cleanup_process_groups,
    _active_process_groups,
)


class TestRegisterProcessGroup:
    def test_register_adds_pgid(self):
        # Arrange
        _active_process_groups.clear()

        # Act
        register_process_group(12345)

        # Assert
        assert 12345 in _active_process_groups

        # Cleanup
        _active_process_groups.clear()

    def test_unregister_removes_pgid(self):
        # Arrange
        _active_process_groups.clear()
        _active_process_groups.add(12345)

        # Act
        unregister_process_group(12345)

        # Assert
        assert 12345 not in _active_process_groups

    def test_unregister_ignores_missing_pgid(self):
        # Arrange
        _active_process_groups.clear()

        # Act / Assert - should not raise
        unregister_process_group(99999)


class TestCleanupProcessGroups:
    def test_kills_all_registered_groups(self):
        # Arrange
        _active_process_groups.clear()
        _active_process_groups.update({111, 222})

        # Act
        with patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            cleanup_process_groups()

        # Assert
        assert mock_killpg.call_count == 2
        called_pgids = {c.args[0] for c in mock_killpg.call_args_list}
        assert called_pgids == {111, 222}
        # All called with SIGKILL (9)
        for c in mock_killpg.call_args_list:
            assert c.args[1] == 9
        assert len(_active_process_groups) == 0

    def test_handles_already_dead_process(self):
        # Arrange
        _active_process_groups.clear()
        _active_process_groups.add(111)

        # Act - should not raise even when process is already dead
        with patch(
            "agent_shell.process_cleanup.os.killpg",
            side_effect=ProcessLookupError,
        ):
            cleanup_process_groups()

        # Assert
        assert len(_active_process_groups) == 0

    def test_clears_registry_after_cleanup(self):
        # Arrange
        _active_process_groups.clear()
        _active_process_groups.update({111, 222, 333})

        # Act
        with patch("agent_shell.process_cleanup.os.killpg"):
            cleanup_process_groups()

        # Assert
        assert len(_active_process_groups) == 0
