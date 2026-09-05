# Interactive harness proof of concept

Experimental, opt-in work on `feat/interactive-harness-poc`. Adapters: Codex, Claude Code,
Pi, OpenCode, Copilot CLI, Cursor, and Grok. Requires Linux/WSL, Python 3.12+, tmux, and
installed/authenticated harnesses.

## What you see

The actual interactive harness runs directly on tmux's controlling terminal. Its own code draws
the UI and handles keyboard input. AgentShell neither reconstructs the interface nor renders
structured events into the visible pane. A new terminal emulator can attach to that same UI.

## Run the demo

From the repository:

```bash
uv run python examples/interactive_demo.py --agent all
```

The controller creates one owned tmux session with a separate window for each harness. It prints
an attach command for another terminal, and a switch-client command for use inside tmux. Move
between harnesses with tmux's next-window binding (normally prefix+n).

To show one harness beside your current conversation, run inside tmux:

```bash
uv run python examples/interactive_demo.py --agent pi --split-pane
```

`--split-pane` creates a side-by-side split and keeps keyboard focus in the controller pane.
Use prefix+arrow to enter the agent UI. `/quit` in the controller closes only the agent's pane;
the original pane and its running process stay alive. Exiting the harness retains its screen
until the controller closes it. Prefix+d detaches the whole tmux client.
This toggle requires a single `--agent` and cannot be combined with `--new-terminal`.

Keep the controller running. Resolve any login, trust, or permission prompts directly in each
real UI. Then type into the UI yourself, or send a prompt from the controller:

```text
codex Reply with a short greeting. Do not use tools.
claude_code Reply with a short greeting. Do not use tools.
pi Reply with a short greeting. Do not use tools.
/key codex C-c
/quit
```

Prompts sent this way are live model requests. `/quit` closes all demo-owned sessions. Ctrl-C
in the controller also cleans up. Detaching a tmux client does not stop the controller or harness.
Only send controller input when the destination UI is ready: input is literal keyboard input,
so a trust dialog or an unfinished manually typed prompt changes how the harness will handle it.

Select one harness with `--agent codex`, `--agent claude_code`, or `--agent pi`. For one harness,
`--model` accepts its native model selector. `--cwd` selects the project. No initial prompt is sent
unless `--prompt` is supplied. `--new-terminal` uses the existing terminal-launcher discovery to
open an attached client; launcher availability depends on the desktop/WSL setup. The printed
attach command also works in a manually opened terminal.

## Library entry point

```python
from agent_shell import TmuxExecutionHost, TmuxPlacement
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell

shell = AgentShell(
    AgentType.PI,
    execution_host=TmuxExecutionHost(TmuxPlacement.current_session()),
)
async with await shell.open_interactive("/path/to/project") as session:
    # Open the created window and resolve any startup dialogs first.
    await session.terminal.send_text("Say hello", submit=True)
    async for event in session.events():
        print(event)
        if event.type == "result":
            break
```

`open_interactive()` also accepts `prompt`, `model`, `effort`, and `session_id`. Native harness
permission prompts remain enabled. Headless `execute()`, `stream()`, `health_check()`, model
discovery, and MCP management keep their existing behavior. Interactive operation is a separate
session API; it does not yet provide every headless option or guaranteed response collection.

For a split, use `TmuxPlacement.split_pane()` in place of `current_session()` above.
Both headless and interactive launches support it. The target is the caller's `TMUX_PANE`,
so changing the active pane elsewhere does not redirect the launch. `focus=True` selects the
new pane immediately; the default keeps the caller focused. It fails clearly outside tmux.
All seven adapters share this host option without harness-specific changes.

## Shared architecture

- `AgentShell.open_interactive()` validates and delegates using optional structural protocols.
  Unsupported hosts or adapters raise explicitly; there is no silent headless fallback.
- `TmuxExecutionHost.launch_interactive()` reuses `TmuxPlacement` and owns the terminal lifecycle.
  `launch()` retains its existing pipe-based behavior for headless adapters.
- `TmuxTerminalSession` supplies literal paste, control keys, screen capture, resize, wait, and
  idempotent close. The child has a foreground process group and a real controlling terminal.
  An owner-lifetime pipe also releases the pane if the controller process dies.
- `InteractiveSession` shares JSON event-file reading, partial-record buffering, and cleanup.
  There is one event reader per session; reopening a reader preserves pending normalized events.
- Each adapter prepares its own CLI arguments and event parser. Pi's extension lives in its
  adapter; the Python hook recorder is shared and knows nothing about harness formats.

No app server, harness SDK, or new Python runtime dependency is used. Only `NoIsolation` is
currently supported, consistent with the existing visible hosts. Other requested policies fail
before launching. Temporary files live in private per-run directories; user config files are not
edited. Normal close removes those files. A hard controller crash can leave the event directory
in `/tmp`; the terminal worker still terminates its harness and removes its pane.

## Structured information available

Inspect `session.capabilities` before interpreting metrics. The existing `StreamEvent` defaults
remain zero for unavailable metrics; the capability set distinguishes missing data from reported
zero values. Events include manually submitted turns as well as controller input.

| Harness | Source | Current structured support |
|---|---|---|
| Codex | Launch-local `notify` command | Session ID, final text, successful turn notification |
| Claude Code | Launch-local hooks | Session ID, tools, provisional text, stop requested |
| Pi | Launch-local extension | Session ID, text, tool names, settled result, output tokens, cost |
| OpenCode | Launch-local plugin | Session ID, text, tool names, idle status |
| Copilot CLI | Launch-local plugin hooks | Session ID, tool names, provisional stop status |
| Cursor | Launch-local plugin hook | New-session ID only; responses stay in native UI |
| Grok | Native session JSONL | Session ID, text, tools, result, output tokens, duration |

Codex's notification provides no usage or failure event. It overrides an existing `notify` command
for this invocation; chaining the user's notification is not implemented. Claude's Stop hook is
provisional because another hook can request continuation, so it emits `status=stop_requested`,
never a successful `result`. Claude usage and reliable terminal turn success remain unavailable.
Claude 2.1.77 rejects the newer `StopFailure` hook and skips the entire settings file, so this POC
does not register it.

Pi reuses the existing event parser and waits for `agent_settled`, accumulating usage across
automatic retries. Pi versions without that extension event will not produce a settled result.
No timer or screen-silence heuristic substitutes for it. Streaming thinking is not exposed by
this initial interactive API.

Process exit produces a separate `process_exit` event, never a successful turn result. A missing
pane without recorded exit status reports an error. Turn events keep streaming while the UI stays
open. Use a caller timeout when waiting for an event that a harness may not emit. After process
exit the reader drains records for 250 ms; notifications arriving later are not guaranteed.

Grok pins a UUID and reads only its native `updates.jsonl` through the shared reader. Resume
requires a UUID and starts at the current end of the log, so old answers are not replayed. Native
session files are never deleted. `end_turn` is successful; cancellations and unknown stop reasons
are failures. Cost is currently unavailable. Switching to a different conversation inside the
Grok UI is not followed by the observer; open a new AgentShell session to select another UUID.

OpenCode preserves inherited `OPENCODE_CONFIG_CONTENT` while adding its local plugin. Its idle
signal is a status, not proof of success. Copilot Stop can be blocked by another hook and exposes
no response text here. Cursor's installed CLI delivers `sessionStart` but not response/stop plugin
hooks. New sessions advertise `session_id`; resumed sessions have no structured capabilities
because Cursor also omits `sessionStart` on resume. Cursor rejects plugin
paths with fewer than three non-root components, so its plugin uses a nested temporary directory.

Interactive `allowed_tools` currently supports Grok's native tool names only. Other adapters
reject a supplied whitelist explicitly. Grok keeps normal permissions enabled; this whitelist
is a tool filter, not an OS sandbox. Empty lists are rejected rather than treated as unrestricted.

## Validation and live-demo status

Behavioral tests use real tmux terminals and local subprocesses, substituting only the external
harness boundary. They exercise all seven adapters, the actual hook recorder and JavaScript plugins,
terminal input/resize/interrupt, process status, ownership cleanup, controller death, concurrent
close, reader continuation, and the demo controller. No inference is involved in these tests.

Local E2E tests launch the actual harnesses with a small prompt, check observable events and
native UI output, and ensure the process stays interactive. These use existing credentials and
incur model usage. They are not included in CI. Resolve native startup dialogs before running.

```bash
AGENTSHELL_E2E_TRUST_WORKSPACE=1 uv run pytest tests/e2e/test_interactive_harness_e2e.py -q
```

The optional environment flag accepts only Copilot's folder-trust dialog for the selected
workspace, for that test session. It does not approve tool execution or persist folder trust.
Claude's local run is currently blocked by a pre-existing error in `~/.claude/settings.json`;
those user settings were not changed or bypassed.

Validation on this branch: 1,009 unit/integration tests passed. Real prompt E2E checks passed
for Codex, Pi, OpenCode, Copilot CLI, Cursor and Grok; Claude is blocked as described above.
A later Pi rerun was blocked by an installed extension asking to set up its Python runtime;
that external setup was not changed.

Run regression checks:

```bash
uv run pytest tests/unit tests/integration -q
```

The real-terminal tests skip if tmux is absent. Pi and OpenCode plugin fixtures require Node.js.

References consulted: [Codex advanced configuration][codex], [Claude hooks][claude], and
[Pi extensions][pi], alongside installed CLI help and Pi's installed extension declarations.

[codex]: https://developers.openai.com/codex/config-advanced
[claude]: https://code.claude.com/docs/en/hooks
[pi]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md

Additional sources: [OpenCode plugins](https://opencode.ai/docs/plugins/),
[Copilot hooks](https://docs.github.com/en/copilot/reference/hooks-reference),
[Cursor hooks](https://cursor.com/docs/hooks), and Grok's installed
`~/.grok/docs/user-guide/17-sessions.md`, alongside the installed CLI help and loader code.
