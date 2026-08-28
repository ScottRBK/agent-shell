"""Fixtures shared by the whole suite."""
import pytest

from agent_shell import process_cleanup


@pytest.fixture(autouse=True)
def isolate_agentshell_environment(monkeypatch):
    """Keep deployment-level AgentShell configuration explicit in each test."""
    monkeypatch.delenv("AGENTSHELL_ISOLATION_POLICY", raising=False)


@pytest.fixture(autouse=True)
def isolate_process_group_registry():
    """Give every test an isolated guardian registry and close anything it leaks."""
    saved = dict(process_cleanup._guardians)
    process_cleanup._guardians.clear()
    try:
        yield
    finally:
        # This runs on assertion failures too. Leaving a guardian's pipe open would leave its
        # process blocked and could hide the test failure behind process cleanup at interpreter
        # exit.
        for process in list(process_cleanup._guardians):
            process_cleanup.kill_process_group(process)
        process_cleanup._guardians.update(saved)
