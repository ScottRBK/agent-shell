"""AgentShell interactive flow with real tmux and controlled external CLI fixtures."""

import asyncio
import os
from pathlib import Path
import shutil
import sys
from contextlib import aclosing

import pytest

from agent_shell import TmuxExecutionHost
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell
from tests.integration.test_interactive_terminal import isolated_tmux, screen_contains  # noqa: F401


def install_cli(tmp_path, monkeypatch, name, source):
    binary = tmp_path / name
    binary.write_text("#!/usr/bin/env python3\n" + source)
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


async def test_codex_real_ui_and_completion_share_one_session(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: only the external harness is substituted; terminal and notification helper are real.
    install_cli(tmp_path, monkeypatch, "codex", '''
import json, os, subprocess, sys, tomllib
assert all(os.isatty(fd) for fd in (0, 1, 2))
assert "exec" not in sys.argv and "--json" not in sys.argv
notify = next(tomllib.loads(arg)["notify"] for arg in sys.argv if arg.startswith("notify="))
print("CODEX UI READY", flush=True)
prompt = input()
subprocess.run(notify + [json.dumps({
    "type": "agent-turn-complete", "thread-id": "codex-session", "turn-id": "turn-1",
    "last-assistant-message": "Answer: " + prompt,
})], check=True)
print("CODEX UI STILL OPEN", flush=True)
input()
''')
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act
    session = await shell.open_interactive(str(tmp_path))
    async with session:
        await screen_contains(session.terminal, "CODEX UI READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.type == "result":
                    break
        screen = await screen_contains(session.terminal, "CODEX UI STILL OPEN")

        # Assert
        assert [(e.type, e.content) for e in events] == [
            ("system", ""), ("text", "Answer: hello"), ("result", "ok"),
        ]
        assert events[0].session_id == "codex-session"
        assert "CODEX UI STILL OPEN" in screen
        assert session.terminal.returncode is None
        assert "output_tokens" not in session.capabilities
    assert session.terminal.closed


async def test_pi_extension_waits_for_settled_and_reports_usage(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: an external Pi stand-in loads the actual extension and exercises its public hooks.
    if not shutil.which("node"):
        pytest.skip("Pi extension integration test requires node")
    binary = tmp_path / "pi"
    binary.write_text('''#!/usr/bin/env node
const {pathToFileURL} = require("node:url");
const readline = require("node:readline");
(async () => {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw Error("not a terminal");
  if (process.argv.includes("--print")) throw Error("not interactive");
  const extension = process.argv[process.argv.indexOf("--extension") + 1];
  const handlers = new Map();
  const ctx = {sessionManager: {getSessionId: () => "pi-session"}};
  (await import(pathToFileURL(extension))).default({on: (name, fn) => handlers.set(name, fn)});
  const emit = async (type, extra = {}) => handlers.get(type)?.({type, ...extra}, ctx);
  await emit("session_start");
  console.log("PI UI READY");
  const input = readline.createInterface({input: process.stdin});
  input.once("line", async () => {
    await emit("agent_start");
    await emit("agent_end", {messages: [{role: "assistant", stopReason: "error",
      errorMessage: "transient", usage: {output: 2, cost: {total: 0.01}}}]});
    await emit("agent_start");
    await emit("message_update", {assistantMessageEvent: {type: "text_end", content: "Pi answer"}});
    await emit("agent_end", {messages: [{role: "assistant", stopReason: "stop",
      usage: {output: 8, cost: {total: 0.02}}}]});
    await emit("tool_execution_start", {toolName: "read"});
    await emit("agent_settled");
    console.log("PI UI STILL OPEN");
  });
})();
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    shell = AgentShell(AgentType.PI, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        await screen_contains(session.terminal, "PI UI READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.type == "result":
                    break

        # Assert: the failed retry must not be mistaken for final completion or lose its usage.
        assert events[0].session_id == "pi-session"
        assert [e.content for e in events if e.type == "text"] == ["Pi answer"]
        assert events[-1].content == "ok"
        assert all(e.session_id == "pi-session" for e in events)
        assert any(e.type == "tool_use" for e in events)
        assert events[-1].output_tokens == 10
        assert events[-1].cost == pytest.approx(0.03)
        assert {"turn_complete", "output_tokens", "cost"} <= session.capabilities
        assert session.terminal.returncode is None
    assert session.terminal.closed


async def test_claude_hooks_preserve_ui_and_do_not_claim_early_success(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    install_cli(tmp_path, monkeypatch, "claude", '''
import json, os, subprocess, sys
assert all(os.isatty(fd) for fd in (0, 1, 2))
assert "--print" not in sys.argv and "-p" not in sys.argv
settings = json.load(open(sys.argv[sys.argv.index("--settings") + 1]))
# Claude Code 2.1.77 rejects the entire file if any newer hook name is present.
assert set(settings["hooks"]) <= {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"}
def emit(name, **extra):
    hook = settings["hooks"][name][0]["hooks"][0]["command"]
    subprocess.run(hook, shell=True, check=True, input=json.dumps({
        "hook_event_name": name, "session_id": "claude-session", **extra,
    }).encode())
emit("SessionStart")
print("CLAUDE UI READY", flush=True)
input()
emit("Stop", last_assistant_message="Provisional answer", stop_hook_active=False)
print("CLAUDE UI STILL OPEN", flush=True)
input()
''')
    shell = AgentShell(AgentType.CLAUDE_CODE, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        await screen_contains(session.terminal, "CLAUDE UI READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.type == "status":
                    break

        # Assert
        assert [(e.type, e.content) for e in events] == [
            ("system", ""), ("text", "Provisional answer"),
            ("status", "stop_requested"),
        ]
        assert events[0].session_id == "claude-session"
        assert "turn_complete" not in session.capabilities
        assert "output_tokens" not in session.capabilities
        assert session.terminal.returncode is None


async def test_reopening_event_reader_does_not_drop_pending_events(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    install_cli(tmp_path, monkeypatch, "codex", '''
import json, subprocess, sys, time, tomllib
notify = next(tomllib.loads(arg)["notify"] for arg in sys.argv if arg.startswith("notify="))
subprocess.run(notify + [json.dumps({"type": "agent-turn-complete",
    "thread-id": "session", "last-assistant-message": "retained answer"})], check=True)
time.sleep(60)
''')
    shell = AgentShell(AgentType.CODEX, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        async with asyncio.timeout(3):
            async with aclosing(session.events()) as reader:
                first = await anext(reader)
            async with aclosing(session.events()) as reader:
                second = await anext(reader)
                third = await anext(reader)

        # Assert
        assert first.type == "system"
        assert second.content == "retained answer"
        assert third.type == "result"


async def test_demo_controller_sends_prompt_and_cleans_up(isolated_tmux, tmp_path, monkeypatch):
    # Arrange
    install_cli(tmp_path, monkeypatch, "codex", '''
import json, subprocess, sys, tomllib
notify = next(tomllib.loads(arg)["notify"] for arg in sys.argv if arg.startswith("notify="))
for prompt in sys.stdin:
    subprocess.run(notify + [json.dumps({"type": "agent-turn-complete",
        "thread-id": "demo", "last-assistant-message": "Demo received " + prompt.strip()})])
''')
    demo = Path(__file__).parents[2] / "examples" / "interactive_demo.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(demo), "--agent", "codex", "--cwd", str(tmp_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = []

    # Act
    try:
        async with asyncio.timeout(5):
            while line := await process.stdout.readline():
                output.append(line.decode())
                if b"Controller ready" in line:
                    process.stdin.write(b"codex hello\n")
                    await process.stdin.drain()
                if b"Demo received hello" in line:
                    process.stdin.write(b"/quit\n")
                    await process.stdin.drain()
            await process.wait()
        stderr = (await process.stderr.read()).decode()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()

    # Assert
    assert process.returncode == 0, stderr
    assert any("Demo received hello" in line for line in output)
    assert any("Closed all demo sessions" in line for line in output)


async def test_cursor_plugin_reports_only_verified_session_metadata(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: exercise the real plugin files and hook writer in an external CLI fixture.
    install_cli(tmp_path, monkeypatch, "cursor-agent", '''
import json, os, pathlib, subprocess, sys
assert all(os.isatty(fd) for fd in (0, 1, 2))
assert "--print" not in sys.argv
plugin = pathlib.Path(sys.argv[sys.argv.index("--plugin-dir") + 1])
# Cursor refuses extension directories with fewer than three non-root path components.
assert len(plugin.resolve().parts) >= 4
assert json.loads((plugin / ".cursor-plugin/plugin.json").read_text())["name"]
hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
def emit(name, **data):
    for hook in hooks[name]:
        subprocess.run(hook["command"], shell=True, check=True, input=json.dumps({
            "hook_event_name": name, "conversation_id": "cursor-session", **data,
        }), text=True)
print("CURSOR READY", flush=True)
input()
emit("sessionStart")
print("CURSOR STILL OPEN", flush=True)
input()
''')
    shell = AgentShell(AgentType.CURSOR, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        await screen_contains(session.terminal, "CURSOR READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.type == "system":
                    break

        # Assert: native CLI turn hooks are unavailable, so no turn/text capability is promised.
        assert [(e.type, e.content) for e in events] == [
            ("system", ""),
        ]
        assert events[0].session_id == "cursor-session"
        assert session.capabilities == frozenset({"session_id"})
        assert session.terminal.returncode is None


async def test_copilot_plugin_reports_lifecycle_and_tools(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    install_cli(tmp_path, monkeypatch, "copilot", '''
import json, os, pathlib, subprocess, sys
assert all(os.isatty(fd) for fd in (0, 1, 2))
assert "-p" not in sys.argv and "--headless" not in sys.argv
plugin = pathlib.Path(sys.argv[sys.argv.index("--plugin-dir") + 1])
hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
def emit(name, **data):
    for hook in hooks[name]:
        subprocess.run(hook["command"], shell=True, check=True, input=json.dumps({
            "hook_event_name": name, "session_id": "copilot-session", **data,
        }), text=True)
print("COPILOT READY", flush=True)
input()
emit("SessionStart")
emit("PostToolUse", tool_name="Read")
emit("Stop", stop_reason="end_turn")
input()
''')
    shell = AgentShell(AgentType.COPILOT_CLI, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        await screen_contains(session.terminal, "COPILOT READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.content == "stop_requested":
                    break

        # Assert
        assert [(e.type, e.content) for e in events] == [
            ("system", ""), ("tool_use", "Read"), ("status", "stop_requested"),
        ]
        assert events[0].session_id == "copilot-session"
        assert "turn_complete" not in session.capabilities


async def test_opencode_plugin_reports_completed_assistant_text(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: Node loads the actual OpenCode plugin; only the harness is substituted.
    if not shutil.which("node"):
        pytest.skip("OpenCode plugin test requires node")
    binary = tmp_path / "opencode"
    binary.write_text('''#!/usr/bin/env node
const readline = require("node:readline");
(async () => {
  if (!process.stdin.isTTY) throw Error("not a terminal");
  const config = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT);
  if (config.permission.read !== "allow") throw Error("lost inherited config");
  const plugin = (await import(config.plugin.at(-1))).default;
  const hooks = await plugin({});
  console.log("OPENCODE READY");
  readline.createInterface({input: process.stdin}).once("line", async () => {
    const emit = (type, properties) => hooks.event({event: {type, properties}});
    await emit("session.created", {info: {id: "oc-session"}});
    await emit("message.updated", {info: {id: "user", role: "user", sessionID: "oc-session"}});
    await emit("message.part.updated", {part: {id: "u1", messageID: "user", type: "text",
      sessionID: "oc-session", text: "Do not echo this", time: {end: 1}}});
    await emit("message.updated", {info: {id: "assistant", role: "assistant",
      sessionID: "oc-session"}});
    const part = {id: "a1", messageID: "assistant", sessionID: "oc-session", type: "text",
      text: "OpenCode answer", time: {end: 2}};
    await emit("message.part.updated", {part});
    await emit("message.part.updated", {part});
    await emit("session.idle", {sessionID: "oc-session"});
  });
})();
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"permission":{"read":"allow"}}')
    shell = AgentShell(AgentType.OPENCODE, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        await screen_contains(session.terminal, "OPENCODE READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.content == "idle":
                    break

        # Assert: completed text is not echoed or duplicated, idle does not imply success.
        assert [e.content for e in events if e.type == "text"] == ["OpenCode answer"]
        assert events[0].session_id == "oc-session"
        assert not any(e.type == "result" for e in events)


async def test_grok_native_log_delivers_text_and_turn_result(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: delayed creation and split writes model the real append-only session stream.
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    install_cli(tmp_path, monkeypatch, "grok", '''
import json, os, pathlib, sys, time
assert all(os.isatty(fd) for fd in (0, 1, 2))
assert "-p" not in sys.argv and "stdio" not in sys.argv
sid = sys.argv[sys.argv.index("--session-id") + 1]
print("GROK READY", flush=True)
input()
path = pathlib.Path(os.environ["GROK_HOME"]) / "sessions" / "workspace" / sid / "updates.jsonl"
path.parent.mkdir(parents=True)
def emit(update):
    line = json.dumps({"method": "session/update", "params": {"sessionId": sid, "update": update}})
    with path.open("a") as f:
        f.write(line[:15]); f.flush(); time.sleep(0.07)
        f.write(line[15:] + "\\n")
emit({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hello"}})
emit({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Grok answer"}})
emit({"sessionUpdate": "turn_completed", "stop_reason": "end_turn",
      "usage": {"outputTokens": 12, "reasoningTokens": 4}, "elapsed_ms": 1500})
input()
''')
    shell = AgentShell(AgentType.GROK, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path)) as session:
        await screen_contains(session.terminal, "GROK READY")
        await session.terminal.send_text("hello", submit=True)
        events = []
        async with asyncio.timeout(5):
            async for event in session.events():
                events.append(event)
                if event.type == "result":
                    break

        # Assert
        assert [e.content for e in events if e.type == "text"] == ["Grok answer"]
        assert events[-1].content == "ok"
        assert events[-1].output_tokens == 12
        assert events[-1].duration == 1.5
        assert events[-1].session_id
        assert session.terminal.returncode is None


async def test_grok_resume_skips_history_and_reports_cancelled_turn(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    import json
    sid = "11111111-1111-4111-8111-111111111111"
    root = tmp_path / "grok-home"
    log = root / "sessions" / "workspace" / sid / "updates.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD"},
    }}}) + "\n")
    monkeypatch.setenv("GROK_HOME", str(root))
    install_cli(tmp_path, monkeypatch, "grok", '''
import json, os, pathlib, sys
sid = sys.argv[sys.argv.index("--resume") + 1]
path = pathlib.Path(os.environ["GROK_HOME"]) / "sessions" / "workspace" / sid / "updates.jsonl"
print("GROK RESUMED", flush=True)
input()
with path.open("a") as f:
    f.write(json.dumps({"params": {"sessionId": sid, "update": {
        "sessionUpdate": "turn_completed", "stop_reason": "cancelled",
    }}}) + "\\n")
input()
''')
    shell = AgentShell(AgentType.GROK, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(str(tmp_path), session_id=sid) as session:
        await screen_contains(session.terminal, "GROK RESUMED")
        await session.terminal.send_text("hello", submit=True)
        async with asyncio.timeout(5), aclosing(session.events()) as events:
            result = await anext(events)

        # Assert: old answer isn't replayed and cancellation never becomes success.
        assert result.type == "result"
        assert result.content == "error"
        assert result.error == "cancelled"
    assert log.exists(), "AgentShell must not delete the harness's own session history"


async def test_grok_interactive_review_can_restrict_native_tools(
    isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange: the native whitelist must reach Grok without enabling blanket approval.
    install_cli(tmp_path, monkeypatch, "grok", '''
import sys
assert sys.argv[sys.argv.index("--tools") + 1] == "read_file,grep,list_dir"
assert "--always-approve" not in sys.argv
print("RESTRICTED REVIEW READY", flush=True)
input()
''')
    shell = AgentShell(AgentType.GROK, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(
        str(tmp_path), allowed_tools=["read_file", "grep", "list_dir"],
    ) as session:
        screen = await screen_contains(session.terminal, "RESTRICTED REVIEW READY")

        # Assert
        assert "RESTRICTED REVIEW READY" in screen


@pytest.mark.parametrize("agent", [a for a in AgentType if a != AgentType.GROK])
async def test_unimplemented_interactive_tool_whitelist_fails_explicitly(agent, tmp_path):
    # Arrange
    shell = AgentShell(agent, execution_host=TmuxExecutionHost())

    # Act / Assert: a review restriction must never be silently ignored.
    with pytest.raises(NotImplementedError, match="allowed_tools"):
        await shell.open_interactive(str(tmp_path), allowed_tools=["read"])


@pytest.mark.parametrize("agent,binary,expected", [
    (AgentType.CURSOR, "cursor-agent", ["--model", "test-model", "--resume=test-session"]),
    (AgentType.COPILOT_CLI, "copilot", ["--model", "test-model", "--resume=test-session"]),
    (AgentType.OPENCODE, "opencode", ["--model", "test-model", "--session", "test-session"]),
])
async def test_interactive_selection_and_initial_prompt_reach_native_cli(
    agent, binary, expected, isolated_tmux, tmp_path, monkeypatch,
):
    # Arrange
    prompt = "literal $HOME `whoami` café"
    source = (
        "import sys\n"
        f"expected = {expected!r}\n"
        "start = sys.argv.index('--model')\n"
        "assert sys.argv[start:start + len(expected)] == expected\n"
        f"assert sys.argv[-1] == {prompt!r}\n"
        "print('SELECTION ACCEPTED', flush=True)\ninput()\n"
    )
    install_cli(tmp_path, monkeypatch, binary, source)
    shell = AgentShell(agent, execution_host=TmuxExecutionHost())

    # Act
    async with await shell.open_interactive(
        str(tmp_path), prompt=prompt, model="test-model", session_id="test-session",
    ) as session:
        screen = await screen_contains(session.terminal, "SELECTION ACCEPTED")

        # Assert
        assert "SELECTION ACCEPTED" in screen
        if agent == AgentType.CURSOR:
            assert session.capabilities == frozenset()
