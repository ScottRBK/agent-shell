"""Tests for the process group cleanup registry (safety net for orphaned processes).

These tests seed `_active_process_groups` with invented pids and do not clean it up themselves.
The autouse `isolate_process_group_registry` fixture in tests/conftest.py empties the registry
before each test and restores it afterwards, on the failure path as well as the passing one.
That matters more than it sounds: the module registers `cleanup_process_groups` with atexit, so
an invented pid left behind by a failing test is handed to the real `os.killpg` when pytest's
interpreter exits. Do not add a `_active_process_groups.clear()` back into a test body — the
fixture is the only mechanism, and a second one that a failure can skip is what caused the bug.
"""
import asyncio
import os
import signal
import sys
import time
from unittest.mock import patch, call, MagicMock

import pytest

from agent_shell.process_cleanup import (
    register_process_group,
    unregister_process_group,
    cleanup_process_groups,
    kill_process_group,
    release_process,
    _active_process_groups,
)


def _wait_until_reaped(pid: int, timeout: float = 5.0) -> bool:
    """True once `pid` is gone from the process table (or is a zombie awaiting its reaper)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().split(") ", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.01)
    return False


class TestRegisterProcessGroup:
    def test_register_adds_pgid(self):
        # Act
        register_process_group(12345)

        # Assert
        assert 12345 in _active_process_groups

    def test_unregister_removes_pgid(self):
        # Arrange
        _active_process_groups.add(12345)

        # Act
        unregister_process_group(12345)

        # Assert
        assert 12345 not in _active_process_groups

    def test_unregister_ignores_missing_pgid(self):
        # Act / Assert - should not raise
        unregister_process_group(99999)


class TestCleanupProcessGroups:
    def test_kills_all_registered_groups(self):
        # Arrange — both pids still lead their own group, i.e. both children are still ours,
        # so both groups must actually be signalled. getpgid is patched to say so; without it
        # the pids resolve to nothing on this machine and the guard would skip them, hiding
        # whether the kill still happens in the case it is meant to.
        _active_process_groups.update({111, 222})

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=lambda pid: pid), \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            cleanup_process_groups()

        # Assert
        assert mock_killpg.call_count == 2
        called_pgids = {c.args[0] for c in mock_killpg.call_args_list}
        assert called_pgids == {111, 222}
        # All called with SIGKILL (9)
        for c in mock_killpg.call_args_list:
            assert c.args[1] == 9
        assert len(_active_process_groups) == 0

    def test_does_not_signal_a_group_the_registered_pid_does_not_lead(self):
        # Arrange — B1 again, on the path that had no guard. The registry holds pids (adapters
        # call register_process_group(process.pid)), and a pid is only ours while the child is
        # unreaped; afterwards os.getpgid() resolves a stranger's group. Every adapter spawns
        # with preexec_fn=os.setsid, so a pid that is still ours leads its own group.
        _active_process_groups.add(555)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=999), \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            cleanup_process_groups()

        # Assert
        mock_killpg.assert_not_called()

    def test_handles_already_dead_process(self):
        # Arrange — the child exited before atexit ran and left nothing behind, so getpgid
        # raises AND the probe for a surviving process group raises too. Both have to say so:
        # a reaped child whose group still has members is a live orphan, not a dead entry.
        _active_process_groups.add(111)

        # Act - should not raise even when process is already dead
        with patch(
            "agent_shell.process_cleanup.os.getpgid",
            side_effect=ProcessLookupError,
        ), patch("agent_shell.process_cleanup.os.killpg",
                 side_effect=ProcessLookupError) as mock_killpg:
            cleanup_process_groups()

        # Assert — the probe happened, no SIGKILL followed, and no stale entry is left.
        assert mock_killpg.call_args_list == [call(111, 0)]
        assert len(_active_process_groups) == 0

    def test_a_failed_kill_does_not_escape(self):
        # Arrange — this runs from atexit, where an escaping exception has nowhere to go. The
        # old body caught only ProcessLookupError and PermissionError, so any other OSError
        # (EINVAL from a bogus pgid, say) aborted the loop and abandoned the rest of the
        # registry.
        _active_process_groups.add(111)

        # Act - must not raise
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=111), \
             patch("agent_shell.process_cleanup.os.killpg",
                   side_effect=OSError(22, "Invalid argument")):
            cleanup_process_groups()

        # Assert
        assert len(_active_process_groups) == 0

    def test_clears_registry_after_cleanup(self):
        # Arrange
        _active_process_groups.update({111, 222, 333})

        # Act — same live-child shape as above, so the clear is not being reached only
        # because the guard skipped every entry.
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=lambda pid: pid), \
             patch("agent_shell.process_cleanup.os.killpg"):
            cleanup_process_groups()

        # Assert
        assert len(_active_process_groups) == 0


class TestKillProcessGroup:
    """Regression coverage for issue #8: cancel() used to unregister only on the kill-success
    path, so a process that had already exited by the time cancel() ran (getpgid raises
    ProcessLookupError) left a stale entry in the registry forever."""

    def test_kills_process_group_and_unregisters(self):
        # Arrange
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
        # Arrange — process exited on its own before cancel() got to it, so getpgid raises and
        # the process group it led is gone with it. killpg is patched rather than left real:
        # the probe now reaches os.killpg, and an unpatched one would signal whatever group on
        # this machine happens to be numbered 12345.
        _active_process_groups.add(12345)

        # Act - should not raise
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=ProcessLookupError), \
             patch("agent_shell.process_cleanup.os.killpg", side_effect=ProcessLookupError):
            kill_process_group(12345)

        # Assert — the registry entry is cleared even though the kill never happened.
        assert 12345 not in _active_process_groups

    def test_unregisters_by_pid_not_pgid(self):
        # Arrange — register_process_group is called with process.pid at spawn time, so
        # unregistering must key off the same pid for symmetry, not whatever getpgid returns.
        _active_process_groups.add(500)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=999), \
             patch("agent_shell.process_cleanup.os.killpg"):
            kill_process_group(500)

        # Assert
        assert 500 not in _active_process_groups


class TestKillProcessGroupTargetsOnlyOurOwnSession:
    """Regression coverage for B1's demonstrated harm.

    kill_process_group resolved the group to signal with os.getpgid(pid) and SIGKILLed
    whatever came back. A pid is only ours for as long as the child is unreaped; once the
    child watcher's os.waitpid() has run, the kernel is free to hand that number to anyone,
    and the getpgid() lookup then resolves a *stranger's* group. An adversarial run forced a
    pid wrap and watched an innocent unrelated process die by signal 9.

    Every adapter spawns with preexec_fn=os.setsid, so a pid that is still ours always leads
    its own session: pgid == pid. Anything else is not our child any more."""

    async def test_does_not_signal_a_group_the_pid_does_not_lead(self):
        # Arrange — a real child spawned WITHOUT setsid sits in the test runner's own process
        # group, which is exactly the shape a recycled pid has: os.getpgid(pid) resolves to a
        # live group that this pid does not lead. os.killpg is patched ONLY so that a
        # regression is reported as a test failure instead of SIGKILLing the test runner.
        child = await asyncio.create_subprocess_exec(
            "sleep", "60", stdout=asyncio.subprocess.DEVNULL,
        )
        assert os.getpgid(child.pid) != child.pid, "child unexpectedly leads its own group"
        _active_process_groups.add(child.pid)

        # Act
        with patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            kill_process_group(child.pid)

        # Assert — no signal at all, and the stale entry is dropped: whatever this pid is
        # now, the child we registered is gone.
        mock_killpg.assert_not_called()
        assert child.pid not in _active_process_groups

        # Cleanup
        child.kill()
        await child.wait()

    async def test_kills_a_real_setsid_child_and_its_grandchild(self):
        # Arrange — the positive control for the guard above, and the reason teardown uses
        # killpg at all: a CLI's own subprocesses live in the session the adapter created with
        # setsid and have to die with it. Real processes, real os.killpg, no patching.
        script = (
            "import subprocess, sys, time; "
            "gc = subprocess.Popen(['sleep', '60']); "
            "print(gc.pid, flush=True); "
            "time.sleep(60)"
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE, preexec_fn=os.setsid,
        )
        grandchild_pid = int((await child.stdout.readline()).strip())
        _active_process_groups.add(child.pid)

        # Act
        kill_process_group(child.pid)

        # Assert
        await child.wait()
        assert child.returncode == -signal.SIGKILL
        assert _wait_until_reaped(grandchild_pid), "grandchild survived the process group kill"
        assert child.pid not in _active_process_groups


class TestKillProcessGroupReachesOrphanedGrandchildren:
    """Coverage for the leak the `getpgid(pid) == pid` guard used to accept.

    A CLI that spawns its own subprocesses and then exits leaves them running in the process
    group it created with setsid. Once the child watcher reaps the CLI, nothing holds its pid,
    os.getpgid() raises, and the old guard skipped the kill — so those grandchildren leaked
    forever. The atexit path is where it bit hardest, because by interpreter exit the child has
    usually been reaped already.

    Closing it costs something, and the tests below pin the behaviour, not a proof of safety.
    A live group N whose leader is gone is our child's orphans OR any other double-forking
    daemon's workers — a process that setsid()s, forks, exits and is reaped leaves exactly this
    shape, and nothing about the group distinguishes the two. Reproduced: `_group_is_ours`
    returned True for an unrelated daemon's group that this registry had never heard of. The
    trade is empirical rather than logical — a scan of this host found 94 live group leaders
    against a single orphaned group — so the second branch adds roughly 1% to the exposure the
    first one already carries, in exchange for reaching orphans that used to leak forever.

    Note also that this only closes the leak on the abandoned path. On normal completion
    release_process unregisters without killing, so surviving grandchildren still leak and
    atexit no longer has an entry to find them by."""

    def test_kills_a_group_that_outlived_its_reaped_leader(self):
        # Arrange — no process holds the pid (getpgid raises) but process group `pid` still
        # answers a signal-0 probe, which is only possible while it has live members.
        _active_process_groups.add(4242)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=ProcessLookupError), \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            kill_process_group(4242)

        # Assert — probe first, then the real kill.
        assert mock_killpg.call_args_list == [call(4242, 0), call(4242, 9)]
        assert 4242 not in _active_process_groups

    def test_does_not_signal_when_the_group_is_gone_too(self):
        # Arrange — the ordinary already-dead case: the child was reaped and took its whole
        # group with it, so the probe raises ESRCH as well. Nothing to kill, nothing to keep.
        _active_process_groups.add(4242)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=ProcessLookupError), \
             patch("agent_shell.process_cleanup.os.killpg",
                   side_effect=ProcessLookupError) as mock_killpg:
            kill_process_group(4242)

        # Assert — the probe, and no SIGKILL behind it.
        assert mock_killpg.call_args_list == [call(4242, 0)]
        assert 4242 not in _active_process_groups

    def test_does_not_signal_a_group_it_has_no_permission_for(self):
        # Arrange — EPERM from the probe means not one member of the group can be signalled by
        # this process. Our own descendants always can be, so the group is somebody else's:
        # a pid recycled onto another user's process that then led a group of its own. Do not
        # escalate to SIGKILL — it could not land, and the target is not ours to aim at.
        _active_process_groups.add(4242)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=ProcessLookupError), \
             patch("agent_shell.process_cleanup.os.killpg",
                   side_effect=PermissionError(1, "Operation not permitted")) as mock_killpg:
            kill_process_group(4242)

        # Assert — probed and rejected, and the entry goes: the child we registered is gone,
        # and no later attempt on this pid could do any better.
        assert mock_killpg.call_args_list == [call(4242, 0)]
        assert 4242 not in _active_process_groups

    async def test_kills_a_real_orphaned_group_end_to_end(self):
        # Arrange — the whole shape with real processes and real signals: a setsid'd child
        # forks a grandchild, exits, and is reaped, leaving a live process group whose leader
        # no longer exists.
        # The grandchild gets DEVNULL rather than inheriting the pipe: asyncio's
        # Process.wait() does not return until every pipe is disconnected, so a grandchild
        # holding the write end would make the await below block for its whole lifetime.
        script = (
            "import subprocess, sys; "
            "gc = subprocess.Popen(['sleep', '60'], stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); "
            "print(gc.pid, flush=True)"
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE, preexec_fn=os.setsid,
        )
        grandchild_pid = int((await child.stdout.readline()).strip())
        await child.wait()
        _active_process_groups.add(child.pid)
        try:
            assert os.getpgid(grandchild_pid) == child.pid, "grandchild left the group"
            try:
                os.getpgid(child.pid)
                pytest.skip("child pid was reused before the assertion under test")
            except ProcessLookupError:
                pass

            # Act
            kill_process_group(child.pid)

            # Assert
            assert _wait_until_reaped(grandchild_pid), "orphaned grandchild survived"
            assert child.pid not in _active_process_groups
        finally:
            # Cleanup — never leak the grandchild, whatever the assertions did.
            try:
                os.kill(grandchild_pid, signal.SIGKILL)
            except OSError:
                pass


class TestKillProcessGroupFailures:
    """Regression coverage for S1: kill_process_group caught only ProcessLookupError, yet
    cleanup_process_groups already caught PermissionError too — the authors knew EPERM
    happens. It is B1's sibling: once a pid is recycled onto another user's process, killpg
    raises EPERM. release_process calls this from a `finally` while a GeneratorExit is in
    flight, where an escaping exception replaces the original one."""

    def test_permission_error_does_not_escape(self):
        # Arrange
        _active_process_groups.add(4242)

        # Act - must not raise
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=4242), \
             patch("agent_shell.process_cleanup.os.killpg",
                   side_effect=PermissionError(1, "Operation not permitted")):
            kill_process_group(4242)

        # Assert
        assert True  # reaching here at all is the assertion

    def test_keeps_the_registry_entry_when_the_kill_failed(self):
        # Arrange — unlike ProcessLookupError (proof the group is gone), a failed kill leaves
        # a group that may well still be running. Dropping the entry removes it from the
        # atexit net as well, turning a recoverable leak into a permanent orphan.
        _active_process_groups.add(4242)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=4242), \
             patch("agent_shell.process_cleanup.os.killpg",
                   side_effect=PermissionError(1, "Operation not permitted")):
            kill_process_group(4242)

        # Assert — still registered, so cleanup_process_groups() gets one more attempt.
        assert 4242 in _active_process_groups

    def test_unexpected_oserror_does_not_escape(self):
        # Arrange — any OSError out of getpgid/killpg has to be contained, not just the two
        # errno values that happen to have been seen so far.
        _active_process_groups.add(4242)

        # Act - must not raise
        with patch("agent_shell.process_cleanup.os.getpgid",
                   side_effect=OSError(22, "Invalid argument")):
            kill_process_group(4242)

        # Assert
        assert 4242 in _active_process_groups


def _fake_process(pid: int, returncode: int | None):
    process = MagicMock()
    process.pid = pid
    process.returncode = returncode
    return process


def _fake_stderr_task(done: bool):
    task = MagicMock()
    task.done.return_value = done
    return task


class TestReleaseProcess:
    """Regression coverage for issue #7: every adapter's stream() did its teardown after the
    stdout read loop, on the normal path only. A consumer that broke out of the `async for`
    early (GeneratorExit at a yield) or any propagating exception skipped it entirely, leaving
    the child running, still in _active_processes and still in the atexit registry.

    Which of kill-vs-unregister it picks is now told to it by the caller (`child_exited`),
    because B1 showed that inferring it from process.returncode is unsound: the child watcher
    reaps the child — freeing its pid for reuse — and only afterwards schedules the callback
    that sets returncode, so `returncode is None` does NOT mean "still alive"."""

    def test_abandoned_live_child_is_killed_not_awaited(self):
        # Arrange — the stream never reached the end of its body, so the consumer walked away
        # from a child that is, as far as anything here can tell, still running. Nobody is
        # draining its pipes any more, so awaiting it could block until a pipe buffer fills,
        # or forever; it has to be killed.
        _active_process_groups.add(4242)
        process = _fake_process(4242, returncode=None)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid", return_value=4242), \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            release_process(process, [process], _fake_stderr_task(done=True),
                            child_exited=False)

        # Assert
        mock_killpg.assert_called_once_with(4242, 9)
        assert 4242 not in _active_process_groups

    def test_completed_stream_is_never_killed_whatever_returncode_says(self):
        # Arrange — B1: the caller awaited the child, so it has definitely exited and its pid
        # may already have been recycled. returncode is deliberately None here, the state the
        # old code read as "still running": the decision must come from the caller's knowledge
        # of the exit path, not from a field the child watcher sets on a later loop turn.
        _active_process_groups.add(4242)
        process = _fake_process(4242, returncode=None)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid") as mock_getpgid, \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            release_process(process, [process], _fake_stderr_task(done=True),
                            child_exited=True)

        # Assert
        mock_killpg.assert_not_called()
        mock_getpgid.assert_not_called()
        assert 4242 not in _active_process_groups

    def test_abandoned_child_already_reaped_is_not_killed(self):
        # Arrange — the narrow residual race the flag cannot cover: the stream really was
        # abandoned mid-flight, but the child exited and its returncode landed before the
        # teardown ran. A set returncode is positive proof the child is gone, so killing would
        # only ever reach a since-reused pid.
        _active_process_groups.add(4242)
        process = _fake_process(4242, returncode=0)

        # Act
        with patch("agent_shell.process_cleanup.os.getpgid") as mock_getpgid, \
             patch("agent_shell.process_cleanup.os.killpg") as mock_killpg:
            release_process(process, [process], _fake_stderr_task(done=True),
                            child_exited=False)

        # Assert
        mock_killpg.assert_not_called()
        mock_getpgid.assert_not_called()
        assert 4242 not in _active_process_groups

    def test_child_exited_is_required(self):
        # Arrange — the whole point of B1's fix is that the exit path is passed in rather than
        # guessed. A caller that forgets must fail loudly, not silently fall back to a guess.
        process = _fake_process(4242, returncode=0)

        # Act / Assert
        try:
            release_process(process, [process], _fake_stderr_task(done=True))
        except TypeError:
            pass
        else:
            raise AssertionError("release_process accepted a call without child_exited")

    def test_removes_process_from_the_active_list(self):
        # Arrange — a process left behind here would later let cancel() killpg a reused PID.
        process = _fake_process(4242, returncode=0)
        other = _fake_process(99, returncode=0)
        active = [other, process]

        # Act
        release_process(process, active, _fake_stderr_task(done=True), child_exited=True)

        # Assert — only the released process goes; a sibling stream's process stays.
        assert active == [other]

    def test_tolerates_process_already_removed_by_a_concurrent_cancel(self):
        # Arrange — cancel() clears _active_processes, so the finally can find the process
        # already gone. An unguarded list.remove would raise ValueError out of the teardown.
        _active_process_groups.add(4242)
        process = _fake_process(4242, returncode=0)

        # Act — empty list, as cancel() would have left it
        release_process(process, [], _fake_stderr_task(done=True), child_exited=True)

        # Assert — no ValueError, and the registry entry still gets cleared.
        assert 4242 not in _active_process_groups

    def test_process_lookup_error_does_not_escape(self):
        # Arrange — the child can exit between the returncode check and the kill, so getpgid
        # raises. A teardown that lets that escape replaces a leak with a crash. killpg is
        # patched because the probe for a surviving group reaches it, and an unpatched one
        # would signal whatever group on this machine happens to be numbered 4242.
        _active_process_groups.add(4242)
        process = _fake_process(4242, returncode=None)

        # Act - should not raise
        with patch("agent_shell.process_cleanup.os.getpgid", side_effect=ProcessLookupError), \
             patch("agent_shell.process_cleanup.os.killpg", side_effect=ProcessLookupError):
            release_process(process, [process], _fake_stderr_task(done=True),
                            child_exited=False)

        # Assert
        assert 4242 not in _active_process_groups

    def test_is_idempotent_across_repeated_calls(self):
        # Arrange — cancel() and the stream's finally can both run teardown for the same child.
        _active_process_groups.add(4242)
        process = _fake_process(4242, returncode=0)
        active = [process]

        # Act - second call must not raise
        release_process(process, active, _fake_stderr_task(done=True), child_exited=True)
        release_process(process, active, _fake_stderr_task(done=True), child_exited=True)

        # Assert
        assert active == []
        assert 4242 not in _active_process_groups

    def test_leaves_a_finished_stderr_drain_task_alone(self):
        # Arrange — on the normal path the drain task was already awaited for the error event.
        process = _fake_process(4242, returncode=0)
        stderr_task = _fake_stderr_task(done=True)

        # Act
        release_process(process, [process], stderr_task, child_exited=True)

        # Assert
        stderr_task.cancel.assert_not_called()

    async def test_cancels_a_pending_stderr_drain_task(self):
        # Arrange — a real task, so this proves the cancellation actually lands rather than
        # that cancel() was called on a mock. An orphan surfaces as "Task was destroyed but
        # it is pending" at loop shutdown.
        process = _fake_process(4242, returncode=0)
        stderr_task = asyncio.ensure_future(asyncio.Event().wait())
        await asyncio.sleep(0)  # let it start

        # Act
        release_process(process, [process], stderr_task, child_exited=True)
        await asyncio.sleep(0.05)  # let the requested cancellation finalize

        # Assert
        assert stderr_task.cancelled()
