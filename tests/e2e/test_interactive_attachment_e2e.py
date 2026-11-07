"""Local-only Codex attachment smoke test; uses credentials and incurs model usage."""

import asyncio
import os
from pathlib import Path
import shutil

import pytest

from agent_shell import TmuxExecutionHost
from agent_shell.interactive_terminal import TmuxTerminalConnection
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell
from tests.integration.test_interactive_terminal import isolated_tmux  # noqa: F401

pytestmark = pytest.mark.e2e


async def observed_reply(terminal, expected):
    screen = ""
    try:
        async with asyncio.timeout(120):
            while expected not in (screen := await terminal.capture_screen()):
                await asyncio.sleep(0.2)
    except TimeoutError:
        pytest.fail(f"Codex did not produce {expected!r}; native UI:\n{screen[-2500:]}")
    return screen


async def test_real_codex_receives_input_through_public_attachment(isolated_tmux):
    # Arrange: use an already-trusted workspace; never approve permission dialogs here.
    if not shutil.which("codex"):
        pytest.skip("codex is not installed")
    cwd = os.environ.get("AGENTSHELL_E2E_CWD", str(Path(__file__).resolve().parents[2]))
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())
    prompt = (
        "Reply only with the concatenation of ATTACH_ and READY. "
        "Do not use tools or modify files."
    )
    async with await shell.open_interactive(
        cwd, prompt=prompt, model="gpt-6-luna", effort="low",
    ) as owner:
        await observed_reply(owner.terminal, "ATTACH_READY")
        assert isinstance(owner.terminal, TmuxTerminalConnection)
        target = owner.terminal.target

        # Act: the acknowledgement is absent from the input, so prompt echo cannot pass.
        async with await shell.attach_interactive(target) as connection:
            await connection.terminal.send_text(
                "Reply only with the concatenation of ATTACH_ and RECEIVED. "
                "Do not use tools or modify files.", submit=True,
            )
            screen = await observed_reply(connection.terminal, "ATTACH_RECEIVED")

        # Assert: the borrower disconnected while the original pane remains usable.
        assert "ATTACH_RECEIVED" in screen
        assert connection.terminal.closed
        assert not owner.terminal.closed
        assert "ATTACH_RECEIVED" in await owner.terminal.capture_screen()
        assert await isolated_tmux("list-panes", "-a", "-F", "#{pane_id}") == target.pane_id
