"""
Module-level process group registry with atexit cleanup.

Safety net for orphaned child processes when the parent exits without
calling cancel() — e.g. when asyncio.run() converts SIGINT into
CancelledError and the KeyboardInterrupt handler never fires.

Every adapter registers its child's process group ID here on subprocess
creation and unregisters it on normal completion or explicit cancel().
If any PGIDs remain at interpreter shutdown, atexit kills them.

`release_process` is the counterpart to that registration: every adapter's
`stream()` calls it from a `finally`, so it covers the exception and
abandoned-consumer paths as well as the normal one.

That `finally` is not, however, a guarantee that teardown has happened by
the time the consumer moves on. CPython does not run an async generator's
`finally` synchronously when a consumer `break`s out of an `async for`; it
schedules the generator's aclose() as a separate async_generator_athrow
task, and the child stays alive and registered until that task gets a turn.

asyncio.run() does give it one. It calls loop.shutdown_asyncgens() before
closing the loop, so the teardown runs — measured: a break-and-abandon under
asyncio.run() leaves the registry empty, while the same coroutine on a
hand-rolled loop closed without shutdown_asyncgens() leaves an entry behind.
So the atexit net is not covering the ordinary program; it covers the loop
nobody shut down properly, plus any exit that never unwinds the generator at
all. A narrower job than it used to claim, and still the reason the net
exists now that `stream()` has a `finally`.
"""
import atexit
import logging
import os

logger = logging.getLogger("agent_shell.process_cleanup")

_active_process_groups: set[int] = set()


def register_process_group(pgid: int) -> None:
    _active_process_groups.add(pgid)


def unregister_process_group(pgid: int) -> None:
    _active_process_groups.discard(pgid)


def _group_is_ours(pid: int) -> bool:
    """True when process group `pid` looks like one this registry is responsible for.

    "Looks like": the second shape below cannot tell our orphans from a stranger's, so this is a
    best-effort identification, not a proof of ownership. See the caveat under that shape.

    Two shapes qualify, and nothing else does.

    `os.getpgid(pid) == pid` — the child is still alive and still leads its own group. Every
    adapter spawns with preexec_fn=os.setsid, so pgid == pid for as long as the child is ours.
    The kernel is free to hand the number to anyone the moment the child watcher's os.waitpid()
    reaps it, and a recycled pid resolves through os.getpgid() to a group we have no business
    killing — an adversarial run forced a pid wrap and watched an unrelated process die by
    signal 9. The check is not a proof of identity (a recycled pid may itself lead a group), but
    it narrows the target from "any process that inherits this number" to "one that also happens
    to be a group leader".

    getpgid() raises ESRCH but `killpg(pid, 0)` succeeds — no process holds `pid` at all, yet
    process group `pid` still has live members. That is the shape our child's orphaned
    grandchildren leave behind: a group numbered N can only be created by the process whose pid
    was N, either by setsid() or setpgid(), and the kernel refuses both a fabricated group
    number and an attempt to join a group in another session.

    It is not only our shape, and it is not a narrower version of the case above — the two do
    not overlap at all, because the first needs pid N alive and this one needs it dead. Any
    double-forking daemon leaves identical remains: setsid(), fork a worker, exit, get reaped,
    and group N is now a stranger's with no leader. Reproduced, not theorised — this function
    returned True for such a group that this registry had never heard of, and the worker died
    by signal 9. This branch can vouch for a total stranger.

    What justifies it is a count, not an argument. A scan of one Linux desktop found 94 live
    process group leaders against a single orphaned group, so the extra exposure is on the
    order of 1% of what the case above already carries — paid to reach orphans that otherwise
    leak forever. Empirically narrow, not logically narrow, and if the recycled-pid exposure is
    ever closed properly both branches should be revisited together.

    A probe that raises is not ours either way: ESRCH means the group is gone, and EPERM means
    no member of it is signalable by us, which our own descendants always are.
    """
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        pass

    try:
        os.killpg(pid, 0)
    except OSError:
        return False
    return True


def kill_process_group(pid: int) -> None:
    """SIGKILL the process group led by `pid`, then drop its registry entry.

    Signals only a group `_group_is_ours` vouches for, which covers both the live child and the
    child that has been reaped out from under its own still-running grandchildren — and, on
    that second shape, a stranger's group that happens to look the same. See `_group_is_ours`.

    Unregisters by pid, matching register_process_group(process.pid) at spawn time rather than
    the getpgid()-derived pgid, and does so even when the process has already exited — issue #8:
    a process that exits on its own right before cancel() runs must not leave a stale entry.

    Never raises. It runs from a `finally` while a GeneratorExit is in flight, where an escaping
    exception masks the one already being handled.
    """
    try:
        if _group_is_ours(pid):
            os.killpg(pid, 9)
    except ProcessLookupError:
        pass  # gone between the check and the kill: nothing left to kill, nothing to recover
    except OSError as e:
        # The group may still be running and we could not signal it (EPERM is what a pid
        # recycled onto another user's process gives). Keep the registry entry so
        # cleanup_process_groups() gets one more attempt at interpreter exit: an orphan
        # dropped from every recovery path is worse than a stale entry.
        logger.warning("Could not kill process group for pid %s: %s", pid, e)
        return

    unregister_process_group(pid)


def release_process(process, active_processes: list, stderr_task, *,
                    child_exited: bool) -> None:
    """Release everything a `stream()` owns for `process`. Safe on every exit path.

    `child_exited` is the caller's own record of which exit path it took: True only once it
    has `await`ed the child, so the child has definitely terminated. It is a required keyword
    argument because this used to be inferred from `process.returncode`, and that inference is
    unsound. CPython's ThreadedChildWatcher._do_waitpid() calls os.waitpid() — reaping the
    child and freeing its pid for the kernel to hand out again — and only afterwards schedules
    the callback that sets `returncode`. Between those two, `returncode is None` describes a
    child that is already gone and a pid that may already belong to someone else.

      - child_exited, or returncode already set -> the child has terminated. Nothing to kill;
        just drop the registry entry. (returncode being set is positive proof of death even
        though its absence proves nothing, so it is worth a second look.) Note what this path
        does NOT do: a CLI that left subprocesses of its own running is not signalled, and
        dropping the entry also takes those grandchildren out of the atexit net, so they leak
        with nothing left to find them by. Deliberate for now — an adapter may legitimately
        leave a server running — and tracked as its own issue. The orphaned-group recovery in
        `_group_is_ours` applies to the abandoned path below, not to this one.
      - otherwise -> the stream was abandoned mid-flight and, as far as anything here can
        tell, the child is still running. Nobody is draining its pipes any more, so
        `await process.wait()` here could block until a pipe buffer fills, or forever. Kill
        the group instead.

    The second case routinely fires on a child that is already dead, and that is the norm
    rather than a residual race. Measured over 25 runs of the documented break-and-then-work
    pattern, with 0.15s of synchronous work after the `break`: 25/25 teardowns took the kill
    branch, and 25/25 found the pid already reaped and freed. CPython queues the generator's
    async_generator_athrow task AT the `break`, ahead of the child watcher's callback that sets
    `returncode`, while the watcher's os.waitpid() runs on its own thread — so the pid is
    handed back to the kernel while `returncode` still reads None, and no synchronous check
    here can tell. On essentially every break-and-then-work usage, `_group_is_ours` is the only
    thing standing between this call and whoever holds that number now.

    Deliberately synchronous: this runs while a generator is being closed or cancelled, where an
    await can hang or raise and turn cleanup into a second failure. The child is reaped by
    asyncio's child watcher, so no wait() is needed to avoid a zombie.

    Idempotent, so it cannot conflict with a concurrent `cancel()` doing the same teardown: the
    list removal is guarded, `unregister_process_group` discards, and `kill_process_group`
    swallows an already-dead child.
    """
    if not stderr_task.done():
        stderr_task.cancel()

    if process in active_processes:
        active_processes.remove(process)

    if child_exited or process.returncode is not None:
        unregister_process_group(process.pid)
    else:
        kill_process_group(process.pid)


def cleanup_process_groups() -> None:
    """Kill every registered process group. Called by atexit.

    Delegates to kill_process_group so both teardown paths share one rule about what may be
    signalled and one policy for what to do when the kill fails. This used to killpg() the
    registered number unguarded, which mattered more after kill_process_group started keeping
    the registry entry on a failed kill: that deliberately routes recovery traffic here, so an
    unguarded body here would hand every recycled pid a second chance to kill a stranger.

    The entries are pids, not getpgid()-derived pgids — adapters register process.pid at spawn
    and spawn with preexec_fn=os.setsid, so pgid == pid while the child is ours. By interpreter
    exit that number proves very little on its own: the child has usually been reaped and the
    pid may belong to anyone, so `_group_is_ours` is doing the load-bearing work and signalling
    the registered number directly would be reckless.

    Expect this to find nothing most runs. Every path that completes unregisters as it goes —
    including the normal one, which unregisters without killing — so what survives to here is
    only what teardown never reached. An orphaned group with a dead leader can turn up, but it
    is the exception, not the reason for delegating.

    Never raises: atexit has nowhere to report an exception, and one bad entry must not abandon
    the rest of the registry. kill_process_group swallows everything, and the final clear runs
    regardless — at interpreter exit there is no later attempt for a kept entry to serve.
    """
    for pid in list(_active_process_groups):
        kill_process_group(pid)
    _active_process_groups.clear()


atexit.register(cleanup_process_groups)
