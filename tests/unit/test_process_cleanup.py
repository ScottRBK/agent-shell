"""Tests for the process group cleanup registry (safety net for orphaned processes)."""
from unittest.mock import patch, call

from agent_shell.process_cleanup import (
    register_process_group,
    unregister_process_group,
    cleanup_process_groups,
    kill_process_group,
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


class TestKillProcessGroup:
    """Regression coverage for issue #8: cancel() used to unregister only on the kill-success
    path, so a process that had already exited by the time cancel() ran (getpgid raises
    ProcessLookupError) left a stale entry in the registry forever."""

    def test_kills_process_group_and_unregisters(self):
        # Arrange
        _active_process_groups.clear()
        _active_process_groups.add(12345)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=12345) as mock_getpgid, \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            kill_process_group(12345)

        # Assert
        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(12345, 9)
        assert 12345 not in _active_process_groups

    def test_unregisters_even_when_process_already_exited(self):
        # Arrange — process exited on its own before cancel() got to it, so getpgid raises.
        _active_process_groups.clear()
        _active_process_groups.add(12345)

        # Act - should not raise
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=ProcessLookupError):
            kill_process_group(12345)

        # Assert — the registry entry is cleared even though the kill never happened.
        assert 12345 not in _active_process_groups

    def test_unregisters_by_pid_not_pgid(self):
        # Arrange — register_process_group is called with process.pid at spawn time, so
        # unregistering must key off the same pid for symmetry, not whatever getpgid returns.
        _active_process_groups.clear()
        _active_process_groups.add(500)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=999), \
             patch("agent_shell.process_cleanup.os.killpg"):
            kill_process_group(500)

        # Assert
        assert 500 not in _active_process_groups
