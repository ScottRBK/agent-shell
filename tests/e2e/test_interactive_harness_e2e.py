"""Local-only real harness checks. Prompt tests use the configured account and incur usage.

The working directory must already be trusted in each harness. Set
AGENTSHELL_E2E_TRUST_WORKSPACE=1 to accept Copilot's folder trust for this session only.
Tool permission dialogs are never auto-approved.
"""

import asyncio
import os
from pathlib import Path
import shutil

import pytest

from agent_shell import TmuxExecutionHost
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell
from tests.integration.test_interactive_terminal import isolated_tmux  # noqa: F401

pytestmark = pytest.mark.e2e

HARNESSES = [
    (AgentType.CODEX, "codex"), (AgentType.CLAUDE_CODE, "claude"), (AgentType.PI, "pi"),
    (AgentType.CURSOR, "cursor-agent"), (AgentType.COPILOT_CLI, "copilot"),
    (AgentType.OPENCODE, "opencode"), (AgentType.GROK, "grok"),
]


@pytest.mark.parametrize("agent,binary", HARNESSES, ids=[a.value for a, _ in HARNESSES])
async def test_real_interactive_prompt(agent, binary, isolated_tmux):
    # Arrange: native auth/config and a trusted workspace; unique answer absent from prompt.
    if not shutil.which(binary):
        pytest.skip(f"{binary} is not installed")
    cwd = os.environ.get("AGENTSHELL_E2E_CWD", str(Path(__file__).resolve().parents[2]))
    shell = AgentShell(agent, execution_host=TmuxExecutionHost())
    prompt = (
        "Reply only with the concatenation of AGENTSHELL_ and E2E_OK. "
        "Do not use tools or modify files."
    )
    events = []

    async def observe(session):
        async for event in session.events():
            events.append(event)

    # Act
    options = {"model": "gpt-5.6-luna", "effort": "low"} if agent == AgentType.CODEX else {}
    async with await shell.open_interactive(cwd, prompt=prompt, **options) as session:
        observer = asyncio.create_task(observe(session))
        try:
            async with asyncio.timeout(120):
                while True:
                    screen = await session.terminal.capture_screen()
                    if (agent == AgentType.COPILOT_CLI
                            and os.environ.get("AGENTSHELL_E2E_TRUST_WORKSPACE") == "1"
                            and "Confirm folder trust" in screen and cwd in screen
                            and "1. Yes" in screen):
                        await session.terminal.send_key("Enter")
                        await asyncio.sleep(0.5)
                        continue
                    text = "".join(e.content for e in events if e.type == "text")
                    answer_seen = "AGENTSHELL_E2E_OK" in (
                        text if "text" in session.capabilities else screen
                    )
                    lifecycle_seen = any(e.session_id for e in events)
                    if "turn_complete" in session.capabilities:
                        lifecycle_seen = any(e.type == "result" for e in events)
                    if answer_seen and lifecycle_seen:
                        break
                    if session.terminal.returncode is not None:
                        pytest.fail(f"{agent.value} exited: {screen[-2000:]}")
                    await asyncio.sleep(0.2)
        except TimeoutError:
            pytest.fail(f"{agent.value}: no observed reply; native UI:\n{screen[-2500:]}")
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)

        # Assert: a reply is observed while the actual interactive harness remains alive.
        assert answer_seen
        assert any(e.session_id for e in events)
        assert not any(e.type == "error" for e in events)
        assert session.terminal.returncode is None
        if "turn_complete" in session.capabilities:
            assert any(e.type == "result" and e.content == "ok" for e in events)
    assert session.terminal.closed


async def test_real_cursor_resumes_requested_conversation(isolated_tmux):
    # Arrange: a newer decoy conversation detects accidentally resuming the latest session.
    from uuid import uuid4
    if not shutil.which("cursor-agent"):
        pytest.skip("cursor-agent is not installed")
    cwd = os.environ.get("AGENTSHELL_E2E_CWD", str(Path(__file__).resolve().parents[2]))
    shell = AgentShell(AgentType.CURSOR, execution_host=TmuxExecutionHost())
    tokens = ["amber-" + uuid4().hex[:12], "violet-" + uuid4().hex[:12]]
    target_id = None
    for token in tokens:
        prompt = (
            f"Remember this secret phrase: {token}. "
            "Reply with the concatenation of CURSOR_ and READY. Do not use tools."
        )
        async with await shell.open_interactive(cwd, prompt=prompt) as session:
            async with asyncio.timeout(60):
                async for event in session.events():
                    if event.session_id:
                        if target_id is None:
                            target_id = event.session_id
                        break
                while "CURSOR_READY" not in await session.terminal.capture_screen():
                    await asyncio.sleep(0.2)

    # Act: native Cursor emits no SessionStart on resume; observe the actual UI response.
    prompt = "Reply only with the secret phrase I asked you to remember. Do not use tools."
    async with await shell.open_interactive(cwd, prompt=prompt, session_id=target_id) as session:
        try:
            async with asyncio.timeout(60):
                while tokens[0] not in (screen := await session.terminal.capture_screen()):
                    await asyncio.sleep(0.2)
        except TimeoutError:
            pytest.fail(f"Cursor resume did not recall the target conversation:\n{screen}")

        # Assert: correct remembered text, not the newer decoy conversation's phrase.
        assert tokens[0] in screen
        assert tokens[1] not in screen
        assert session.terminal.returncode is None
