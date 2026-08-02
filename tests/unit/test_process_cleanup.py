"""Unit tests for guardian-backed process cleanup decisions."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agent_shell.process_cleanup import (
    _GroupGuardian,
    _guardians,
    cleanup_process_groups,
    create_grouped_process,
    kill_process_group,
    release_process,
    release_process_group,
)


def _fake_process(pid: int = 4242, returncode: int | None = None):
    process = MagicMock()
    process.pid = pid
    process.returncode = returncode
    return process


def _fake_stderr_task(done: bool):
    task = MagicMock()
    task.done.return_value = done
    return task


def _fake_guardian(pid: int, control_fd: int):
    process = MagicMock()
    process.pid = pid
    return _GroupGuardian(process=process, control_fd=control_fd)


class TestGuardianCommands:
    def test_kill_uses_the_registered_guardian_and_forgets_it(self):
        # Arrange
        process = _fake_process()
        guardian = _fake_guardian(pid=100, control_fd=200)
        _guardians[process] = guardian

        # Act
        with patch("agent_shell.process_cleanup._send_guardian_command") as send:
            kill_process_group(process)

        # Assert
        send.assert_called_once_with(guardian, b"K")
        assert process not in _guardians

    def test_release_stops_the_guardian_without_killing_the_group(self):
        # Arrange
        process = _fake_process()
        guardian = _fake_guardian(pid=100, control_fd=200)
        _guardians[process] = guardian

        # Act
        with patch("agent_shell.process_cleanup._send_guardian_command") as send:
            release_process_group(process)

        # Assert
        send.assert_called_once_with(guardian, b"R")
        assert process not in _guardians

    def test_missing_guardian_never_falls_back_to_a_numeric_group_signal(self):
        # Arrange
        process = _fake_process(pid=12345)

        # Act
        with patch("agent_shell.process_cleanup.os.killpg") as killpg:
            kill_process_group(process)

        # Assert
        killpg.assert_not_called()

    def test_repeated_cleanup_is_idempotent(self):
        # Arrange
        process = _fake_process()

        # Act / Assert
        kill_process_group(process)
        kill_process_group(process)
        release_process_group(process)


class TestGroupedProcessCreation:
    async def test_cli_spawn_failure_closes_the_guardian(self):
        # Arrange
        guardian = _fake_guardian(pid=100, control_fd=200)
        error = OSError("spawn failed")

        # Act / Assert
        with patch("agent_shell.process_cleanup._start_guardian", return_value=guardian), \
             patch("asyncio.create_subprocess_exec", side_effect=error), \
             patch("agent_shell.process_cleanup._send_guardian_command") as send:
            with pytest.raises(OSError, match="spawn failed"):
                await create_grouped_process(["missing"], cwd="/tmp")

        send.assert_called_once_with(guardian, b"K")


class TestReleaseProcess:
    def test_completed_stream_releases_instead_of_killing(self):
        # Arrange
        process = _fake_process(returncode=None)
        active = [process]

        # Act
        with patch("agent_shell.process_cleanup.release_process_group") as release, \
             patch("agent_shell.process_cleanup.kill_process_group") as kill:
            release_process(
                process,
                active,
                _fake_stderr_task(done=True),
                child_exited=True,
            )

        # Assert
        release.assert_called_once_with(process)
        kill.assert_not_called()
        assert active == []

    def test_abandoned_live_stream_kills_instead_of_awaiting(self):
        # Arrange
        process = _fake_process(returncode=None)
        active = [process]

        # Act
        with patch("agent_shell.process_cleanup.release_process_group") as release, \
             patch("agent_shell.process_cleanup.kill_process_group") as kill:
            release_process(
                process,
                active,
                _fake_stderr_task(done=True),
                child_exited=False,
            )

        # Assert
        kill.assert_called_once_with(process)
        release.assert_not_called()
        assert active == []

    def test_observed_returncode_is_positive_proof_the_child_exited(self):
        # Arrange
        process = _fake_process(returncode=0)

        # Act
        with patch("agent_shell.process_cleanup.release_process_group") as release, \
             patch("agent_shell.process_cleanup.kill_process_group") as kill:
            release_process(
                process,
                [process],
                _fake_stderr_task(done=True),
                child_exited=False,
            )

        # Assert
        release.assert_called_once_with(process)
        kill.assert_not_called()

    def test_child_exited_is_required(self):
        # Arrange
        process = _fake_process(returncode=0)

        # Act / Assert
        with pytest.raises(TypeError):
            release_process(process, [process], _fake_stderr_task(done=True))

    async def test_pending_stderr_task_is_cancelled(self):
        # Arrange
        process = _fake_process(returncode=0)
        stderr_task = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)

        # Act
        release_process(process, [process], stderr_task, child_exited=True)
        await asyncio.sleep(0)

        # Assert
        assert stderr_task.cancelled()


class TestAtexitCleanup:
    def test_cleanup_kills_every_registered_exact_process(self):
        # Arrange
        first = _fake_process(pid=1)
        second = _fake_process(pid=2)
        _guardians[first] = _fake_guardian(pid=10, control_fd=11)
        _guardians[second] = _fake_guardian(pid=20, control_fd=21)

        # Act
        with patch(
            "agent_shell.process_cleanup.kill_process_group",
            side_effect=lambda process: _guardians.pop(process),
        ) as kill:
            cleanup_process_groups()

        # Assert
        assert kill.call_args_list == [call(first), call(second)]
        assert _guardians == {}
