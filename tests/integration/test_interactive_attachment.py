"""Public attachment behaviour against external processes on isolated tmux servers."""

import asyncio
import os
import shutil
import sys

import pytest

from agent_shell import TmuxExecutionHost, TmuxTarget, TmuxUnavailableError
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell
from tests.integration.test_interactive_terminal import isolated_tmux, screen_contains  # noqa: F401


async def external_pane(tmux):
    """Start an interactive stand-in outside AgentShell's ownership."""
    code = (
        "import os, sys\n"
        "print('READY:' + str(os.getpid()), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('REPLY:' + str(os.getpid()) + ':' + line.rstrip(), flush=True)\n"
    )
    return await tmux(
        "new-session", "-d", "-P", "-F", "#{pane_id}", "--", sys.executable, "-c", code,
    )


@pytest.mark.parametrize("agent_type", list(AgentType))
async def test_attach_send_disconnect_and_reconnect_preserves_external_process(
    isolated_tmux, agent_type,
):
    # Arrange: the original process is not launched by AgentShell.
    pane = await external_pane(isolated_tmux)
    target = await TmuxTarget.capture(pane)
    shell = AgentShell(agent_type, execution_host=TmuxExecutionHost())
    message = "literal $HOME `whoami` café"

    # Act
    async with await shell.attach_interactive(target) as connection:
        await screen_contains(connection.terminal, f"READY:{target.pane_pid}")
        await connection.terminal.send_text(message, submit=True)
        reply = await screen_contains(connection.terminal, f"REPLY:{target.pane_pid}:{message}")
    async with await shell.attach_interactive(target) as reconnected:
        await reconnected.terminal.send_text("still alive", submit=True)
        later_reply = await screen_contains(
            reconnected.terminal, f"REPLY:{target.pane_pid}:still alive",
        )

    # Assert
    assert message in reply
    assert f"REPLY:{target.pane_pid}:still alive" in later_reply
    assert connection.terminal.closed
    assert reconnected.terminal.closed
    assert await isolated_tmux("list-panes", "-a", "-F", "#{pane_id}") == pane
    with pytest.raises(RuntimeError, match="closed"):
        await connection.terminal.send_text("must not arrive", submit=True)


async def test_capture_current_target_can_be_used_outside_its_tmux_environment(
    isolated_tmux, monkeypatch, tmp_path,
):
    # Arrange: capture in the agent pane, then send from an unrelated environment.
    pane = await external_pane(isolated_tmux)
    socket = await isolated_tmux("display-message", "-p", "#{socket_path}")
    monkeypatch.setenv("TMUX", f"{socket},0,0")
    monkeypatch.setenv("TMUX_PANE", pane)
    target = await TmuxTarget.current()
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act: change the socket root too, so the fixture's -L label cannot hide a missing -S.
    with monkeypatch.context() as watcher_environment:
        watcher_environment.setenv("TMUX", "/nonexistent/other-server,0,0")
        watcher_environment.setenv("TMUX_PANE", "%999999")
        watcher_environment.setenv("TMUX_TMPDIR", str(tmp_path))
        async with await shell.attach_interactive(target) as connection:
            await connection.terminal.send_text("explicit target", submit=True)
            reply = await screen_contains(connection.terminal, ":explicit target")

    # Assert
    assert f"REPLY:{target.pane_pid}:explicit target" in reply
    assert target.socket_path == socket
    assert target.pane_id == pane


async def test_launched_terminal_exposes_a_target_without_giving_away_ownership(
    isolated_tmux, tmp_path,
):
    # Arrange
    host = TmuxExecutionHost()
    shell = AgentShell(AgentType.CODEX, execution_host=host)
    command = [sys.executable, "-c", "print('READY', flush=True); print(input()); input()"]

    # Act
    async with await host.launch_interactive(command, str(tmp_path)) as owned:
        await screen_contains(owned, "READY")
        async with await shell.attach_interactive(owned.target) as connection:
            await connection.terminal.send_text("borrowed input", submit=True)
        screen = await screen_contains(owned, "borrowed input")

        # Assert: disconnecting the borrower does not close the owner.
        assert "borrowed input" in screen
        assert not owned.closed
        assert owned.returncode is None
    assert owned.closed


async def test_nondefault_isolation_is_rejected_for_attachment(isolated_tmux):
    # Arrange
    from agent_shell.execution import IsolationUnavailableError, LinuxPidNamespaceIsolation

    target = await TmuxTarget.capture(await external_pane(isolated_tmux))
    shell = AgentShell(
        AgentType.CODEX, execution_host=TmuxExecutionHost(),
        isolation_policy=LinuxPidNamespaceIsolation(),
    )

    # Act / Assert: attaching cannot apply a new isolation boundary to an existing process.
    with pytest.raises(IsolationUnavailableError, match="existing"):
        await shell.attach_interactive(target)


@pytest.mark.parametrize("change", ["removed", "dead", "respawned", "server-restarted"])
@pytest.mark.parametrize("operation", ["attach", "send_text", "send_key", "capture_screen"])
async def test_stale_target_fails_without_touching_replacement(
    isolated_tmux, change, operation,
):
    # Arrange
    pane = await external_pane(isolated_tmux)
    target = await TmuxTarget.capture(pane)
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())
    connection = None if operation == "attach" else await shell.attach_interactive(target)
    if change == "server-restarted":
        await isolated_tmux("kill-server")
        replacement = await external_pane(isolated_tmux)
        assert replacement == pane  # Deliberately exercise reused numeric IDs.
    elif change in {"respawned", "dead"}:
        await isolated_tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
        code = "print('REPLACEMENT', flush=True); input()" if change == "respawned" else "pass"
        await isolated_tmux("respawn-pane", "-k", "-t", pane, "--", sys.executable, "-c", code)
        if change == "dead":
            async with asyncio.timeout(5):
                while await isolated_tmux("display-message", "-p", "-t", pane,
                                         "#{pane_dead}") != "1":
                    await asyncio.sleep(0.02)
    else:
        # Keep the server alive so this case tests disappearance, not a server restart.
        await external_pane(isolated_tmux)
        await isolated_tmux("kill-pane", "-t", pane)

    # Act / Assert
    try:
        with pytest.raises(TmuxUnavailableError):
            if connection is None:
                connection = await shell.attach_interactive(target)
            elif operation == "send_text":
                await connection.terminal.send_text("MUST_NOT_ARRIVE", submit=True)
            elif operation == "send_key":
                await connection.terminal.send_key("Enter")
            else:
                await connection.terminal.capture_screen()
        if change in {"respawned", "server-restarted"}:
            screen = await isolated_tmux("capture-pane", "-p", "-t", pane)
            assert "MUST_NOT_ARRIVE" not in screen
    finally:
        if connection is not None:
            await connection.close()


async def test_capture_rejects_a_missing_pane_without_starting_a_server(tmp_path):
    # Arrange
    socket = tmp_path / "missing.sock"

    # Act / Assert
    with pytest.raises(TmuxUnavailableError):
        await TmuxTarget.capture("%0", socket_path=str(socket))
    assert not socket.exists()


@pytest.mark.parametrize("exit_kind", ["exception", "cancelled"])
async def test_context_exit_and_repeat_close_leave_external_process_alive(isolated_tmux, exit_kind):
    # Arrange
    target = await TmuxTarget.capture(await external_pane(isolated_tmux))
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())
    connection = await shell.attach_interactive(target)
    entered = asyncio.Event()

    async def use_connection():
        async with connection:
            entered.set()
            if exit_kind == "exception":
                raise ValueError("caller failed")
            await asyncio.Event().wait()

    # Act
    task = asyncio.create_task(use_connection())
    await entered.wait()
    if exit_kind == "cancelled":
        task.cancel()
    with pytest.raises(ValueError if exit_kind == "exception" else asyncio.CancelledError):
        await task
    await asyncio.gather(connection.close(), connection.close())
    async with await shell.attach_interactive(target) as reconnected:
        await reconnected.terminal.send_text("survived", submit=True)
        screen = await screen_contains(reconnected.terminal, f"REPLY:{target.pane_pid}:survived")

    # Assert
    assert connection.terminal.closed
    assert f"REPLY:{target.pane_pid}:survived" in screen
    with pytest.raises(RuntimeError, match="closed"):
        await connection.terminal.send_key("Enter")
    with pytest.raises(RuntimeError, match="closed"):
        await connection.terminal.capture_screen()


async def test_attachment_does_not_require_interactive_launch_support(isolated_tmux):
    # Arrange: a real custom host with attachment, but no interactive launch method.
    from agent_shell.execution import NativeExecutionHost

    class AttachmentOnlyHost(NativeExecutionHost):
        async def attach_interactive(self, target):
            return await TmuxExecutionHost().attach_interactive(target)

    target = await TmuxTarget.capture(await external_pane(isolated_tmux))
    shell = AgentShell(AgentType.CODEX, execution_host=AttachmentOnlyHost())

    # Act
    async with await shell.attach_interactive(target) as connection:
        await connection.terminal.send_text("attach only", submit=True)
        screen = await screen_contains(connection.terminal, ":attach only")

    # Assert
    assert f"REPLY:{target.pane_pid}:attach only" in screen


async def test_attached_terminal_preserves_literal_input_and_explicit_submission(isolated_tmux):
    # Arrange
    target = await TmuxTarget.capture(await external_pane(isolated_tmux))
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.attach_interactive(target) as connection:
        with pytest.raises(ValueError, match="control characters"):
            await connection.terminal.send_text("bad\x1binput", submit=True)
        with pytest.raises(ValueError, match="Unsupported terminal key"):
            await connection.terminal.send_key("anything")
        await connection.terminal.send_text("not submitted")
        before = await screen_contains(connection.terminal, "not submitted")
        await connection.terminal.send_key("Enter")
        after = await screen_contains(connection.terminal, f"REPLY:{target.pane_pid}:not submitted")

    # Assert
    assert "REPLY:" not in before
    assert f"REPLY:{target.pane_pid}:not submitted" in after
    assert "bad" not in after


def wrap_tmux(tmp_path, monkeypatch, source):
    """Emulate external CLI conditions, forwarding actual commands to the isolated tmux."""
    original = shutil.which("tmux")
    wrapper = tmp_path / "tmux"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\noriginal = {original!r}\n" + source,
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


async def test_attachment_and_launch_work_without_tmux_32_flags(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: tmux before 3.2 rejects -N. All accepted commands still run on real tmux.
    pane = await external_pane(isolated_tmux)
    wrap_tmux(tmp_path, monkeypatch, '''
if "-N" in sys.argv[1:]:
    sys.stderr.write("tmux: unknown option -- N\\n")
    sys.exit(1)
os.execv(original, [original, *sys.argv[1:]])
''')
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act
    target = await TmuxTarget.capture(pane)
    async with await shell.attach_interactive(target) as connection:
        await connection.terminal.send_text("compatible", submit=True)
        reply = await screen_contains(connection.terminal, ":compatible")
    async with await shell.execution_host.launch_interactive(
        [sys.executable, "-c", "print('OWNED_READY', flush=True); input()"], str(tmp_path),
    ) as owned:
        screen = await screen_contains(owned, "OWNED_READY")

    # Assert
    assert f"REPLY:{target.pane_pid}:compatible" in reply
    assert "OWNED_READY" in screen
    assert owned.closed


@pytest.mark.parametrize("stage", ["load-buffer", "paste-buffer"])
async def test_cancelling_input_cleans_buffer_without_submitting_or_retrying(
    isolated_tmux, tmp_path, monkeypatch, stage,
):
    # Arrange: hold the CLI response after the real load/paste so cancellation is deterministic.
    target = await TmuxTarget.capture(await external_pane(isolated_tmux))
    started = tmp_path / "stage-finished"
    release = tmp_path / "release"
    wrap_tmux(tmp_path, monkeypatch, f'''
import subprocess, time
from pathlib import Path
stage = {stage!r}
if any(arg == stage or arg.startswith(stage + " ") for arg in sys.argv[1:]):
    result = subprocess.run([original, *sys.argv[1:]])
    if result.returncode == 0:
        Path({str(started)!r}).touch()
        while not Path({str(release)!r}).exists():
            time.sleep(0.02)
    sys.exit(result.returncode)
os.execv(original, [original, *sys.argv[1:]])
''')
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.attach_interactive(target) as connection:
        await screen_contains(connection.terminal, "READY:")
        sending = asyncio.create_task(connection.terminal.send_text("CANCEL_INPUT", submit=True))
        try:
            async with asyncio.timeout(5):
                while not started.exists():
                    await asyncio.sleep(0.02)
            sending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sending
            screen = await connection.terminal.capture_screen()
            buffers = await isolated_tmux("list-buffers", "-F", "#{buffer_name}")
        finally:
            release.touch()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)

    # Assert: paste may already have happened, but cancellation must not submit it or leak a buffer.
    assert "REPLY:" not in screen
    assert ("CANCEL_INPUT" in screen) == (stage == "paste-buffer")
    assert buffers == ""
