"""Validation at the public attachment and target-capture boundary."""

import pytest

from agent_shell import (
    HerdrExecutionHost,
    TerminalWindowExecutionHost,
    TmuxExecutionHost,
    TmuxTarget,
    TmuxUnavailableError,
)
from agent_shell.execution import NativeExecutionHost
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


@pytest.mark.parametrize("host_type", [
    NativeExecutionHost, HerdrExecutionHost, TerminalWindowExecutionHost,
])
async def test_unsupported_hosts_reject_attachment(host_type):
    # Arrange
    shell = AgentShell(AgentType.CODEX, execution_host=host_type())

    # Act / Assert
    with pytest.raises(NotImplementedError, match="does not support attaching"):
        await shell.attach_interactive(object())


async def test_tmux_rejects_another_target_type():
    # Arrange
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act / Assert
    with pytest.raises(TypeError, match="TmuxTarget"):
        await shell.attach_interactive("%50")


@pytest.mark.parametrize("pane", ["", "0", "session:0.1", "%1; kill-server", "%١", "-t"])
async def test_capture_requires_an_exact_pane_id(pane, monkeypatch):
    # Arrange: invalid input must fail validation even without tmux installed.
    monkeypatch.setenv("PATH", "")

    # Act / Assert
    with pytest.raises(ValueError, match="pane"):
        await TmuxTarget.capture(pane)


@pytest.mark.parametrize("socket", ["", "relative.sock", "/tmp/tab\t.sock", "/tmp/null\0.sock"])
async def test_capture_requires_an_absolute_printable_socket_path(socket, monkeypatch):
    # Arrange
    monkeypatch.setenv("PATH", "")

    # Act / Assert
    with pytest.raises(ValueError, match="socket"):
        await TmuxTarget.capture("%0", socket_path=socket)


@pytest.mark.parametrize("field,value", [
    ("server_pid", 0), ("server_started", -1), ("pane_pid", "1}"), ("pane_pid", True),
    ("pane_id", "%1; kill-server"), ("socket_path", "relative.sock"),
])
def test_reconstructed_target_validates_identity_fields(field, value):
    # Arrange: targets may be transferred between watcher processes as dataclass fields.
    fields = dict(socket_path="/tmp/test.sock", pane_id="%0", server_pid=1,
                  server_started=1, pane_pid=2)
    fields[field] = value

    # Act / Assert
    with pytest.raises((ValueError, TypeError)):
        TmuxTarget(**fields)


async def test_current_target_requires_both_tmux_environment_variables(monkeypatch):
    # Arrange
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    # Act / Assert
    with pytest.raises(TmuxUnavailableError, match="TMUX and TMUX_PANE"):
        await TmuxTarget.current()


async def test_missing_tmux_is_reported_without_fallback(monkeypatch):
    # Arrange
    target = TmuxTarget("/tmp/test.sock", "%0", 1, 1, 2)
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())
    monkeypatch.setenv("PATH", "")

    # Act / Assert
    with pytest.raises(TmuxUnavailableError, match="tmux.*executable"):
        await shell.attach_interactive(target)
    with pytest.raises(TmuxUnavailableError, match="tmux.*executable"):
        await TmuxTarget.capture("%0")
