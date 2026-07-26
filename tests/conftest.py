"""Fixtures shared by the whole suite."""
import pytest

from agent_shell import process_cleanup


@pytest.fixture(autouse=True)
def isolate_process_group_registry():
    """Hand every test an empty `_active_process_groups`, and take back whatever it leaves.

    This is a safety measure, not a convenience. `process_cleanup` registers
    `cleanup_process_groups` with atexit, so any number still in that module global when
    pytest's interpreter exits is handed to the real `os.killpg`. Tests seed it with invented
    pids — 111, 4242, 12345, 54321 — all far below this host's pid_max of 99999, and by the end
    of a test run a number like that is as likely to belong to a stranger as to nothing at all.
    `_group_is_ours` will even vouch for a stranger that leads its own group. An adversarial run
    inside a PID namespace watched an innocent process holding pid 4242 get SIGKILLed by one
    failing test.

    Teardown after `yield` is the whole point: pytest runs it whether the test passed, failed,
    or raised, which a cleanup statement written at the end of a test body does not — that line
    is skipped by the very failures that most need it. So the tests no longer clear the registry
    themselves. One mechanism, applied everywhere, that a failure cannot step over.

    It restores the snapshot rather than asserting the registry is empty. Some tests are *about*
    an entry outliving teardown: a kill that fails with EPERM deliberately keeps its entry so
    the atexit pass gets one more attempt, and an emptiness assertion would fail exactly the
    tests that pin that behaviour. Restoring makes a leak impossible rather than merely
    reported, which is the stronger of the two anyway.

    Autouse across the whole suite, not just this module's tests: `test_process_group_
    registration.py`, `test_adapter_transport.py` and the integration lifecycle tests all seed
    the same global, and the hazard is in the global, not in any one test file.

    Known consequence: a test that ends with a *live* child still registered loses the atexit
    net for that child, since the entry is dropped here rather than at interpreter exit. The
    adapters unregister on every exit path, so that state means the test leaked — and dropping
    the entry is better than a fixture that hands numbers it cannot vouch for to os.killpg,
    which is the bug this replaces.
    """
    saved = set(process_cleanup._active_process_groups)
    process_cleanup._active_process_groups.clear()
    try:
        yield
    finally:
        # try/finally as well as post-yield teardown: if the fixture generator is ever closed
        # by the garbage collector instead of by pytest, GeneratorExit lands on the yield and
        # this still runs.
        process_cleanup._active_process_groups.clear()
        process_cleanup._active_process_groups.update(saved)
