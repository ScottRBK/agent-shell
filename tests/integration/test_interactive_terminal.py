"""Real terminal behavior through the execution host's public interactive boundary."""

import asyncio
import fcntl
import os
import shutil
import sys
import tempfile
from pathlib import Path
import subprocess

import pytest

from agent_shell import TmuxExecutionHost, TmuxPlacement
from agent_shell.execution import IsolationUnavailableError, LinuxPidNamespaceIsolation
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


@pytest.fixture
def isolated_tmux(monkeypatch):
    real_tmux = shutil.which("tmux")
    if not real_tmux:
        pytest.skip("interactive terminal integration tests require tmux")
    with tempfile.TemporaryDirectory(prefix="as-tmux-test-") as directory:
        # Every command, including worker cleanup, explicitly selects this test's server.
        wrapper = Path(directory) / "tmux"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"os.execv({real_tmux!r}, [{real_tmux!r}, '-L', 'test', *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("TMUX_TMPDIR", directory)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("TMUX_PANE", raising=False)

        async def command(*args):
            process = await asyncio.create_subprocess_exec(
                str(wrapper), *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            assert process.returncode == 0, stderr.decode()
            return stdout.decode().strip()

        try:
            yield command
        finally:
            subprocess.run(
                [str(wrapper), "kill-server"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5,
            )


@pytest.fixture
async def current_tmux_terminal(isolated_tmux, tmp_path, monkeypatch):
    async with await TmuxExecutionHost().launch_interactive(
        [sys.executable, "-c",
         "print('ORIGINAL', flush=True); print('ORIGINAL:' + input(), flush=True); input()"],
        str(tmp_path),
    ) as original:
        await screen_contains(original, "ORIGINAL")
        socket = await isolated_tmux("display-message", "-p", "#{socket_path}")
        monkeypatch.setenv("TMUX", f"{socket},0,0")
        monkeypatch.setenv("TMUX_PANE", original.pane_id)
        yield original


@pytest.mark.parametrize("focus", [False, True])
@pytest.mark.parametrize("direction", [None, "right", "down"])
async def test_split_accepts_input_and_preserves_original_pane(
    current_tmux_terminal, isolated_tmux, tmp_path, focus, direction,
):
    # Arrange
    original = current_tmux_terminal
    options = {"direction": direction} if direction is not None else {}
    host = TmuxExecutionHost(TmuxPlacement.split_pane(focus=focus, **options))

    # Act
    async with await host.launch_interactive(
        [sys.executable, "-c", "print('READY', flush=True); print('REPLY:' + input()); input()"],
        str(tmp_path),
    ) as child:
        await screen_contains(child, "READY")
        await child.send_text("hello split", submit=True)
        screen = await screen_contains(child, "REPLY:hello split")
        active = await isolated_tmux("display-message", "-p", "-t", original.window_id,
                                     "#{pane_id}")
        layout = await isolated_tmux("list-panes", "-t", original.window_id,
                                     "-F", "#{pane_id} #{pane_left} #{pane_top}")
        assert child.window_id == original.window_id
        assert active == (child.pane_id if focus else original.pane_id)
        panes = [line.split() for line in layout.splitlines()]
        assert len(panes) == 2
        assert panes[0][0] == original.pane_id
        assert panes[1][0] == child.pane_id
        if direction == "down":
            assert panes[0][1] == panes[1][1]
            assert int(panes[0][2]) < int(panes[1][2])
        else:
            assert panes[0][2] == panes[1][2]
            assert int(panes[0][1]) < int(panes[1][1])
    await original.send_text("still alive", submit=True)
    original_screen = await screen_contains(original, "ORIGINAL:still alive")

    # Assert
    assert "REPLY:hello split" in screen
    assert "ORIGINAL:still alive" in original_screen
    assert await isolated_tmux("list-panes", "-t", original.window_id,
                               "-F", "#{pane_id}") == original.pane_id


async def test_split_resize_preserves_window_size(current_tmux_terminal, isolated_tmux, tmp_path):
    # Arrange
    original = current_tmux_terminal
    window_size = await isolated_tmux("display-message", "-p", "-t", original.window_id,
                                      "#{window_width} #{window_height}")
    async with await TmuxExecutionHost(TmuxPlacement.split_pane()).launch_interactive(
        [sys.executable, "-c", "input()"], str(tmp_path),
    ) as child:
        # Act
        await child.resize(columns=30, rows=30)

        # Assert
        assert await isolated_tmux("display-message", "-p", "-t", original.window_id,
                                   "#{window_width} #{window_height}") == window_size
        assert await isolated_tmux("display-message", "-p", "-t", child.pane_id,
                                   "#{pane_width}") == "30"


async def test_failed_split_launch_preserves_original_pane(
    current_tmux_terminal, isolated_tmux, tmp_path,
):
    # Arrange
    from agent_shell import TmuxUnavailableError
    original = current_tmux_terminal
    host = TmuxExecutionHost(TmuxPlacement.split_pane())
    await isolated_tmux("set-option", "-w", "-t", original.window_id, "remain-on-exit", "on")

    # Act
    with pytest.raises(TmuxUnavailableError, match="No such file"):
        await host.launch_interactive([str(tmp_path / "missing-command")], str(tmp_path))
    await original.send_text("still alive", submit=True)

    # Assert
    assert "ORIGINAL:still alive" in await screen_contains(original, "ORIGINAL:still alive")
    assert await isolated_tmux("list-panes", "-t", original.window_id,
                               "-F", "#{pane_id}") == original.pane_id


@pytest.mark.parametrize("interactive", [True, False])
async def test_split_requires_current_tmux_context(isolated_tmux, tmp_path, interactive):
    # Arrange
    host = TmuxExecutionHost(TmuxPlacement.split_pane())
    launch = host.launch_interactive if interactive else host.launch

    # Act / Assert
    from agent_shell import TmuxUnavailableError
    with pytest.raises(TmuxUnavailableError, match="requires running inside tmux"):
        await launch([sys.executable, "-c", "pass"], str(tmp_path))


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("direction", ["right", "down"])
async def test_headless_split_preserves_original_pane(
    current_tmux_terminal, isolated_tmux, tmp_path, cancel, direction,
):
    # Arrange
    original = current_tmux_terminal
    host = TmuxExecutionHost(TmuxPlacement.split_pane(direction=direction))
    code = "import time; print('HEADLESS', flush=True); time.sleep(60)" if cancel else (
        "print('HEADLESS', flush=True)"
    )

    # Act
    handle = await host.launch([sys.executable, "-c", code], str(tmp_path))
    try:
        output = await asyncio.wait_for(handle.stdout.readline(), 5)
        panes = await isolated_tmux("list-panes", "-t", original.window_id, "-F", "#{pane_id}")
        layout = await isolated_tmux("list-panes", "-t", original.window_id,
                                     "-F", "#{pane_left} #{pane_top}")
        if cancel:
            await handle.cancel()
        else:
            await asyncio.wait_for(handle.wait(), 5)
    finally:
        handle.release()
    await original.send_text("still alive", submit=True)

    # Assert
    assert output == b"HEADLESS\n"
    assert len(panes.splitlines()) == 2
    first, second = [list(map(int, line.split())) for line in layout.splitlines()]
    if direction == "down":
        assert first[0] == second[0]
        assert first[1] < second[1]
    else:
        assert first[1] == second[1]
        assert first[0] < second[0]
    assert "ORIGINAL:still alive" in await screen_contains(original, "ORIGINAL:still alive")
    assert await isolated_tmux("list-panes", "-t", original.window_id,
                               "-F", "#{pane_id}") == original.pane_id


async def screen_contains(terminal, text):
    async with asyncio.timeout(5):
        while text not in (screen := await terminal.capture_screen()):
            await asyncio.sleep(0.02)
        return screen


async def test_real_terminal_accepts_input_and_reports_exit(isolated_tmux, tmp_path):
    # Arrange: a real process requires a foreground controlling terminal on all three streams.
    code = (
        "import os, sys; "
        "assert all(os.isatty(fd) for fd in (0, 1, 2)); "
        "assert os.tcgetpgrp(0) == os.getpgrp(); "
        "print('READY', flush=True); "
        "print('REPLY:' + input(), flush=True); "
        "sys.exit(7)"
    )
    host = TmuxExecutionHost()

    # Act
    terminal = await host.launch_interactive([sys.executable, "-c", code], str(tmp_path))
    try:
        await screen_contains(terminal, "READY")
        await terminal.send_text("literal $HOME `whoami` café", submit=True)
        screen = await screen_contains(terminal, "REPLY:")
        status = await asyncio.wait_for(terminal.wait(), 5)
    finally:
        await terminal.close()

    # Assert
    assert "REPLY:literal $HOME `whoami` café" in screen
    assert status == 7
    assert terminal.returncode == 7
    assert terminal.closed
    with pytest.raises(RuntimeError, match="closed"):
        await terminal.send_text("cannot write after close")


async def test_resize_and_interrupt_reach_the_real_process(isolated_tmux, tmp_path):
    # Arrange
    code = (
        "import os, signal, time; "
        "signal.signal(signal.SIGWINCH, "
        "lambda *_: print('SIZE:' + str(os.get_terminal_size()), flush=True)); "
        "print('READY', flush=True); time.sleep(60)"
    )
    terminal = await TmuxExecutionHost().launch_interactive(
        [sys.executable, "-c", code], str(tmp_path),
    )

    # Act
    try:
        await screen_contains(terminal, "READY")
        await terminal.resize(columns=90, rows=25)
        screen = await screen_contains(terminal, "SIZE:")
        await terminal.send_key("C-c")
        status = await asyncio.wait_for(terminal.wait(), 5)
    finally:
        await terminal.close()

    # Assert
    assert "columns=90, lines=25" in screen
    assert status != 0


async def test_unsupported_isolation_fails_before_launch(tmp_path):
    # Arrange / Act / Assert
    with pytest.raises(IsolationUnavailableError, match="only NoIsolation"):
        await TmuxExecutionHost().launch_interactive(
            [sys.executable, "-c", "pass"], str(tmp_path),
            isolation_policy=LinuxPidNamespaceIsolation(),
        )


async def test_close_kills_child_but_preserves_borrowed_session(isolated_tmux, tmp_path):
    # Arrange: own a window in a session that another live terminal still uses.
    original = await TmuxExecutionHost().launch_interactive(
        [sys.executable, "-c", "import time; print('ORIGINAL', flush=True); time.sleep(60)"],
        str(tmp_path),
    )
    host = TmuxExecutionHost(TmuxPlacement.new_window(original.session_name))
    child = await host.launch_interactive(
        [sys.executable, "-c", "import time; print('CHILD', flush=True); time.sleep(60)"],
        str(tmp_path),
    )

    # Act
    try:
        await screen_contains(child, "CHILD")
        await child.close()
        status = await asyncio.wait_for(child.wait(), 2)
        screen = await original.capture_screen()
        original_status = original.returncode
    finally:
        await child.close()
        await original.close()

    # Assert
    assert status < 0
    assert "ORIGINAL" in screen
    assert original_status is None


async def test_agentshell_interactive_exactly_targets_numeric_session(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    real_tmux = shutil.which("tmux")
    session_name = "6"
    create = await asyncio.create_subprocess_exec(
        real_tmux, "-f", "/dev/null", "new-session", "-d", "-s", session_name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, create_stderr = await create.communicate()
    assert create.returncode == 0, create_stderr.decode()

    argv_file = tmp_path / "tmux-argv"
    wrapper = tmp_path / "tmux"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, os, sys\n"
        "args = sys.argv[1:]\n"
        "if 'new-window' in args:\n"
        f"    pathlib.Path({str(argv_file)!r}).write_text('\\0'.join(args))\n"
        f"os.execv({real_tmux!r}, [{real_tmux!r}, *args])\n"
    )
    wrapper.chmod(0o755)
    codex = tmp_path / "codex"
    codex.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    shell = AgentShell(
        AgentType.CODEX,
        execution_host=TmuxExecutionHost(
            placement=TmuxPlacement.new_window(session=session_name)
        ),
    )
    interactive = None

    # Act
    try:
        interactive = await shell.open_interactive(str(tmp_path))
        tmux_args = argv_file.read_text().split("\0")
    finally:
        if interactive is not None:
            await interactive.close()
        cleanup = await asyncio.create_subprocess_exec(
            real_tmux, "-f", "/dev/null", "kill-session", "-t", session_name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()

    # Assert
    assert tmux_args[tmux_args.index("-t") + 1] == "=6:"


async def test_process_exit_finishes_event_stream_without_claiming_turn_success(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    from agent_shell.models.agent import AgentType
    from agent_shell.shell import AgentShell

    binary = tmp_path / "codex"
    binary.write_text("#!/usr/bin/env python3\nraise SystemExit(4)\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        async with asyncio.timeout(3):
            events = [event async for event in session.events()]

    # Assert
    assert [(e.type, e.returncode) for e in events] == [("process_exit", 4)]


async def test_owner_death_removes_its_terminal(isolated_tmux, tmp_path):
    # Arrange: the owner is a real separate process that cannot run graceful Python cleanup.
    code = '''
import asyncio, sys
from agent_shell import TmuxExecutionHost
async def main():
    terminal = await TmuxExecutionHost().launch_interactive(
        [sys.executable, "-c", "import time; time.sleep(60)"], sys.argv[1])
    print(terminal.session_name, flush=True)
    await asyncio.sleep(60)
asyncio.run(main())
'''
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(tmp_path), stdout=asyncio.subprocess.PIPE,
    )
    session_name = (await asyncio.wait_for(owner.stdout.readline(), 5)).decode().strip()
    assert session_name.startswith("agentshell-ui-")

    # Act
    owner.kill()
    await owner.wait()
    try:
        async with asyncio.timeout(3):
            while True:
                probe = await asyncio.create_subprocess_exec(
                    "tmux", "has-session", "-t", session_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                if await probe.wait() != 0:
                    break
                await asyncio.sleep(0.05)
    finally:
        cleanup = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", session_name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()

    # Assert
    assert probe.returncode != 0


@pytest.mark.parametrize("interruption", ["death", "cancel", "bad-receipt"])
async def test_interrupted_startup_removes_pane_with_remain_on_exit(
    current_tmux_terminal, isolated_tmux, tmp_path, interruption,
):
    # Arrange: delay only the external tmux launch receipt, after the pane exists.
    original = current_tmux_terminal
    await isolated_tmux("set-option", "-w", "-t", original.window_id, "remain-on-exit", "on")
    tmux = shutil.which("tmux")
    receipt = tmp_path / "receipt"
    wrapper = tmp_path / "tmux"
    wrapper.write_text(
        f"#!{sys.executable}\nimport pathlib, subprocess, sys, time\n"
        f"result = subprocess.run([{tmux!r}, *sys.argv[1:]], capture_output=True)\n"
        "if 'split-window' in sys.argv and result.returncode == 0:\n"
        f"    pathlib.Path({str(receipt)!r}).write_bytes(result.stdout)\n"
        "    time.sleep(1)\n"
        f"    if {interruption!r} == 'bad-receipt': result.stdout = b'bad receipt'\n"
        "sys.stdout.buffer.write(result.stdout)\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "raise SystemExit(result.returncode)\n"
    )
    wrapper.chmod(0o755)
    code = '''
import asyncio, os, sys
from pathlib import Path
from agent_shell import TmuxExecutionHost, TmuxPlacement, TmuxUnavailableError
async def main():
    task = asyncio.create_task(TmuxExecutionHost(TmuxPlacement.split_pane()).launch_interactive(
        [sys.executable, '-c', 'input()'], sys.argv[1]))
    async with asyncio.timeout(5):
        while not Path(sys.argv[2]).exists():
            await asyncio.sleep(0.01)
    if sys.argv[3] == 'death':
        os._exit(0)
    if sys.argv[3] == 'cancel':
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, TmuxUnavailableError):
        pass
    else:
        raise AssertionError('launch unexpectedly succeeded')
asyncio.run(main())
'''
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")

    # Act
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(tmp_path), str(receipt), interruption, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(owner.communicate(), 8)
        assert owner.returncode == 0, stderr.decode()

        # Assert: inherited remain-on-exit must not leave a dead split behind.
        async with asyncio.timeout(3):
            while await isolated_tmux("list-panes", "-t", original.window_id,
                                     "-F", "#{pane_id}") != original.pane_id:
                await asyncio.sleep(0.02)
        await original.send_text("still alive", submit=True)
        await screen_contains(original, "ORIGINAL:still alive")
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.wait()


@pytest.mark.parametrize("shutdown", [
    "close", "owner-death", "pane-removal", "suspended-close", "suspended-owner-death",
])
@pytest.mark.parametrize("leader_exits", [False, True])
async def test_shutdown_removes_resistant_helper(
    current_tmux_terminal, isolated_tmux, tmp_path, shutdown, leader_exits,
):
    # Arrange: the helper ignores terminal hangup and polite termination, holding a real lock.
    helper = tmp_path / "helper.py"
    lock = tmp_path / "helper.lock"
    release = tmp_path / "release-helper"
    helper.write_text('''
import fcntl, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
with open(sys.argv[1], 'w') as locked:
    fcntl.flock(locked, fcntl.LOCK_EX)
    print('HELPER READY', flush=True)
    deadline = time.monotonic() + 15
    while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
''')
    leader = tmp_path / "leader.py"
    leader.write_text('''
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])
if sys.argv[4] == 'False':
    time.sleep(60)
''')
    close = tmp_path / "close"
    code = '''
import asyncio, sys
from pathlib import Path
from agent_shell import TmuxExecutionHost, TmuxPlacement
async def main():
    terminal = await TmuxExecutionHost(TmuxPlacement.split_pane()).launch_interactive(
        [sys.executable, *sys.argv[1:6]], str(Path(sys.argv[1]).parent))
    async with asyncio.timeout(5):
        while 'HELPER READY' not in await terminal.capture_screen():
            await asyncio.sleep(0.01)
    if sys.argv[5] == 'True':
        await terminal.wait()
    print(terminal.pane_id, flush=True)
    while not Path(sys.argv[6]).exists():
        await asyncio.sleep(0.02)
    await terminal.close()
asyncio.run(main())
'''
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(leader), str(helper), str(lock), str(release),
        str(leader_exits), str(close), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        pane = (await asyncio.wait_for(owner.stdout.readline(), 5)).decode().strip()
        assert pane.startswith("%")
        with lock.open("a") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Act: Ctrl-Z suspends the foreground group before cleanup.
            if shutdown.startswith("suspended-"):
                await isolated_tmux("send-keys", "-t", pane, "C-z")
                shutdown = shutdown.removeprefix("suspended-")
            if shutdown == "owner-death":
                owner.kill()
            elif shutdown == "pane-removal":
                await isolated_tmux("kill-pane", "-t", pane)
            else:
                close.touch()

            # Assert: the helper releases its lock well before its own safety deadline.
            async with asyncio.timeout(3):
                while True:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.02)
        await current_tmux_terminal.send_text("still alive", submit=True)
        await screen_contains(current_tmux_terminal, "ORIGINAL:still alive")
    finally:
        release.touch()
        close.touch()
        if owner.returncode is None and shutdown == "pane-removal":
            owner.kill()
        try:
            await asyncio.wait_for(owner.communicate(), 5)
        except TimeoutError:
            owner.kill()
            await owner.wait()


async def test_concurrent_close_is_idempotent(isolated_tmux, tmp_path):
    # Arrange
    terminal = await TmuxExecutionHost().launch_interactive(
        [sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path),
    )

    # Act
    await asyncio.gather(terminal.close(), terminal.close())

    # Assert
    assert terminal.closed
    assert await terminal.wait() < 0


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("replacement_name", ["owned-work", "owned"])
async def test_close_does_not_target_another_session_after_owned_session_disappears(
    isolated_tmux, current_tmux_terminal, tmp_path, replacement_name, interactive,
):
    # Arrange: retain a sentinel so the server cannot restart and reuse IDs.
    host = TmuxExecutionHost(TmuxPlacement.new_session("owned"))
    launch = host.launch_interactive if interactive else host.launch
    terminal = await launch([sys.executable, "-c", "pass"], str(tmp_path))
    await asyncio.wait_for(terminal.wait(), 5)
    pane = await isolated_tmux("list-panes", "-t", "=owned:", "-F", "#{pane_id}")
    await isolated_tmux("kill-pane", "-t", pane)
    async with await TmuxExecutionHost(
        TmuxPlacement.new_session(replacement_name),
    ).launch_interactive([sys.executable, "-c", "input()"], str(tmp_path)) as replacement:
        # Act
        if interactive:
            await terminal.close()
        else:
            terminal.release()

        # Assert
        assert await isolated_tmux("list-panes", "-t", replacement.pane_id,
                                   "-F", "#{pane_id}") == replacement.pane_id


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("kind", ["session", "window", "pane"])
async def test_close_preserves_replacement_after_server_restart(
    isolated_tmux, tmp_path, monkeypatch, interactive, kind,
):
    # Arrange: restart only the fixture's dedicated server, deliberately reusing resource IDs.
    async def prepare_server():
        if kind == "session":
            return TmuxPlacement.new_session("owned")
        identity = await isolated_tmux(
            "new-session", "-d", "-s", "base", "-P", "-F", "#{pane_id} #{socket_path}",
            "--", sys.executable, "-c", "import time; time.sleep(60)",
        )
        pane, socket = identity.split()
        monkeypatch.setenv("TMUX", f"{socket},0,0")
        monkeypatch.setenv("TMUX_PANE", pane)
        return TmuxPlacement.split_pane() if kind == "pane" else TmuxPlacement.current_session()

    host = TmuxExecutionHost(await prepare_server())
    launch = host.launch_interactive if interactive else host.launch
    terminal = await launch([sys.executable, "-c", "pass"], str(tmp_path))
    await asyncio.wait_for(terminal.wait(), 5)
    old_ids = await isolated_tmux("list-panes", "-a", "-F", "#{session_id} #{window_id} #{pane_id}")
    await isolated_tmux("kill-server")
    await prepare_server()
    async with await host.launch_interactive(
        [sys.executable, "-c", "print('READY', flush=True); input()"], str(tmp_path),
    ) as replacement:
        await screen_contains(replacement, "READY")
        assert await isolated_tmux(
            "list-panes", "-a", "-F", "#{session_id} #{window_id} #{pane_id}",
        ) == old_ids

        # Act
        if interactive:
            await terminal.close()
        else:
            terminal.release()

        # Assert
        assert await isolated_tmux(
            "list-panes", "-a", "-F", "#{session_id} #{window_id} #{pane_id}",
        ) == old_ids


async def test_manually_removed_pane_does_not_hang_waiter(isolated_tmux, tmp_path):
    # Arrange
    terminal = await TmuxExecutionHost().launch_interactive(
        [sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path),
    )

    # Act: emulate a person closing the pane from tmux.
    try:
        process = await asyncio.create_subprocess_exec("tmux", "kill-pane", "-t", terminal.pane_id)
        await process.wait()
        async with asyncio.timeout(2):
            with pytest.raises(RuntimeError, match="terminal disappeared"):
                await terminal.wait()
    finally:
        await terminal.close()


async def test_immediate_process_exit_keeps_exact_status(isolated_tmux, tmp_path):
    # Arrange: the shortest real executable exercises exit during the foreground handoff.
    host = TmuxExecutionHost()

    # Act
    async with await host.launch_interactive(["/bin/true"], str(tmp_path)) as terminal:
        status = await asyncio.wait_for(terminal.wait(), 5)

        # Assert
        assert status == 0


async def test_tmux_kill_timeout_still_releases_owner(isolated_tmux, tmp_path, monkeypatch):
    # Arrange: substitute only the external tmux executable's failing kill operation.
    real_tmux = shutil.which("tmux")
    wrapper = tmp_path / "tmux"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys, time\n"
        "if any(arg.startswith(('kill-session', 'kill-window')) for arg in sys.argv):\n"
        "    time.sleep(60)\n"
        f"os.execv({real_tmux!r}, [{real_tmux!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    # A separate controller ensures failed assertions cannot leak a test-owned FIFO descriptor.
    code = '''
import asyncio, sys
from agent_shell import TmuxExecutionHost
async def main():
    terminal = await TmuxExecutionHost().launch_interactive(
        [sys.executable, '-c', 'import time; time.sleep(60)'], sys.argv[1],
    )
    await terminal.close()
    assert terminal.closed
    await terminal.close()
    # Keep the controller alive: its FIFO release must remove the owned session now.
    async with asyncio.timeout(3):
        while True:
            probe = await asyncio.create_subprocess_exec(
                sys.argv[2], 'has-session', '-t', terminal.session_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            if await probe.wait() != 0:
                break
            await asyncio.sleep(0.05)
    print('CLOSED TWICE', flush=True)
asyncio.run(main())
'''

    # Act
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(tmp_path), real_tmux,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 12)

    # Assert
    assert process.returncode == 0, stderr.decode()
    assert b"CLOSED TWICE" in stdout
