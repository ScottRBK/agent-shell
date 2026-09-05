# Examples

See the [README Execute example](../README.md#execute) for basic usage and session resumption.
The snippets below use `AgentShell` and `AgentType` from these imports:

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType
```

## Execution host and isolation

Existing callers remain unchanged when `AGENTSHELL_ISOLATION_POLICY` is unset. Omitting both
constructor settings then means `NativeExecutionHost()` plus `NoIsolation()`:

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
```

Set a process-wide deployment default without changing Python call sites:

```bash
export AGENTSHELL_ISOLATION_POLICY=linux-pid-namespace
```

The accepted values are `none` and `linux-pid-namespace`. AgentShell reads the value when each
shell is constructed. An empty or unknown value raises `ValueError` rather than silently disabling
isolation. An explicit constructor policy takes precedence over the environment, so a caller can
still force one shell to use `NoIsolation()` or a custom policy.

To protect the AgentShell owner from broad same-user cleanup commands such as `pkill -f`,
explicitly request Linux PID namespace isolation:

```python
from agent_shell.execution import LinuxPidNamespaceIsolation

shell = AgentShell(
    agent_type=AgentType.CLAUDE_CODE,
    isolation_policy=LinuxPidNamespaceIsolation(),
)
```

The policy runs a tiny namespace init as PID 1 and the real CLI as PID 2 or later. Processes in
that child namespace cannot signal AgentShell's ancestor processes. With the default private
`/proc` mount they cannot see those ancestors either. The feature requires Linux, the `unshare`
command, and kernel support for unprivileged user/PID namespaces. If any are unavailable,
launching raises `IsolationUnavailableError`; it never silently falls back.

`LinuxPidNamespaceIsolation()` mounts a private `/proc` view by default, so tools such as `ps`
show namespace PIDs. In a restricted container where that mount is denied but the PID namespace
itself is available, opt into the inherited outer `/proc` view explicitly:

```python
isolation_policy=LinuxPidNamespaceIsolation(mount_proc=False)
```

The PID and signal boundary, PID 1 reaper, and descendant cleanup are retained, but `/proc` and
`ps` can then expose outer PID information that disagrees with the child's `os.getpid()`. The
selected mode is checked during the availability probe and still fails closed when unavailable;
it does not create a private mount namespace. The environment setting
`AGENTSHELL_ISOLATION_POLICY=linux-pid-namespace` always selects the default `mount_proc=True`
mode; use the explicit constructor for the opt-in mode.

This is **direct-signal protection, not a sandbox**. It does not restrict files, credentials,
network access, tools, or resource consumption. Any background descendants are also terminated
when the isolated namespace ends. `execute()`, `stream()`, and `health_check()` use the selected
host/policy; model discovery and MCP configuration remain local management operations.

`NativeExecutionHost` remains the default. Execution hosts are opt-in and do not add Python
dependencies.

> [!WARNING]
> `HerdrExecutionHost`, `TmuxExecutionHost`, and `TerminalWindowExecutionHost` are experimental.
> They are opt-in, and their constructors, placement options, supported platforms, and lifecycle
> behaviour may change in a later minor release as real-world usage expands. Explicit requests
> continue to fail closed when their external prerequisite or requested policy is unavailable;
> AgentShell never silently substitutes `NativeExecutionHost`.

### Herdr execution host (experimental)

An opt-in `HerdrExecutionHost` runs each command in a
uniquely owned Herdr pane while preserving the same adapter API:

```python
from agent_shell import HerdrExecutionHost

shell = AgentShell(
    agent_type=AgentType.CLAUDE_CODE,
    execution_host=HerdrExecutionHost(),
)
```

The `herdr` executable is an optional external prerequisite; AgentShell does not install a Python
Herdr client or silently fall back to native execution. Herdr host v1 supports `NoIsolation()`
only, and supports `DEVNULL` or `PIPE` stdin. A private, standard-library bridge carries the
command, environment, output, status, and cancellation between the host and the Herdr worker, so
secrets are not placed in the worker's launcher arguments. `RunHandle.pid` identifies the bridge
worker, while the target CLI's normal or signal return status is preserved. Every run cleans up
only its own Herdr pane, workspace, and bridge directory. Cleanup is bounded (five seconds by
default; configure `cleanup_timeout=` when constructing the host). Model discovery and MCP
configuration are local management operations and do not create Herdr panes.

On Linux, both the bridge worker and its target use a parent-death guard, so an abrupt Herdr pane
or worker exit cannot leave the target running. Other Unix platforms use the normal bridge
disconnect cleanup but do not provide this kernel-level guard; an abrupt worker or pane death may
therefore require external cleanup.

### tmux execution host (experimental)

For a visible tmux run, opt into `TmuxExecutionHost` (the `tmux` executable itself is an optional
system prerequisite):

```python
from agent_shell import TmuxExecutionHost, TmuxPlacement

shell = AgentShell(
    agent_type=AgentType.CLAUDE_CODE,
    execution_host=TmuxExecutionHost(),
)
```

By default, each run creates and owns one uniquely named tmux session and pane. Placement can be
chosen explicitly: `TmuxPlacement.new_session(name=None)` creates an owned session, while
`TmuxPlacement.new_window(session, focus=False)` borrows an existing session and owns only the
new window. `TmuxPlacement.current_session(focus=False)` is a convenience for creating a window
in the current tmux session and fails clearly when called outside tmux. `focus` defaults to false,
so creating a window does not change the user's active window.

A private, one-shot stdlib bridge keeps stdout and stderr as separate raw streams for the adapter
while mirroring output into the pane; the command, working directory, environment, and prompt are
sent over the private Unix socket, so secrets are not placed in the tmux command line.
`RunHandle.pid` identifies the bridge/worker that owns the lifecycle, while `returncode` is the
target CLI's exact exit or signal status.

Version 1 supports `NoIsolation` and `DEVNULL` or `PIPE` stdin only. Unsupported isolation
policies and arbitrary file descriptors fail closed before a tmux session is created. Completed,
cancelled, abandoned, and interpreter-exit runs clean up their AgentShell-owned resource: a new
session for session placement, or exactly the new window for borrowed-session placement. The host
never kills unrelated tmux sessions or windows. Missing sessions, explicit name collisions, and
unidentifiable windows fail closed without falling back to a new session. If tmux is unavailable,
`TmuxUnavailableError` is raised and AgentShell does not silently fall back to the native host.
Model discovery and MCP configuration remain local management operations.

### Terminal-window execution host (experimental)

To watch a headless run in a new local graphical terminal window, inject the host into
`AgentShell`:

```python
from agent_shell import TerminalWindowExecutionHost
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(
    agent_type=AgentType.CLAUDE_CODE,
    execution_host=TerminalWindowExecutionHost(),
)
```

The host is Linux-focused in v1 and needs an installed terminal emulator plus a usable X11 or
Wayland session. It discovers `x-terminal-emulator`, `gnome-terminal`, `konsole`, `kitty`,
`alacritty`, `foot`, `wezterm`, or `xterm` in that order. These are optional external
prerequisites; AgentShell has no terminal-emulator or Python runtime dependency. A caller can
inject a `TerminalWindowLauncher` strategy (for example, `SubprocessTerminalLauncher`) for a
different emulator or platform. If no launcher or graphical session is available, the request
raises `TerminalWindowUnavailableError` and does not fall back to native execution.
For a simple local override, set `AGENTSHELL_TERMINAL_LAUNCHER` to an executable name or path;
strategies are preferred when custom argument conventions are needed.

Each run opens a one-shot worker. The worker receives the command, environment, and prompt-bearing
CLI arguments over a private mode-0700 directory and Unix socket; only the non-secret socket path
is passed to the terminal launcher. Raw stdout and stderr bytes are forwarded unchanged to
AgentShell and mirrored to the visible terminal. `RunHandle.pid` is the worker PID, and the
target command's exact exit or signal status is returned.

Only `NoIsolation` and `DEVNULL`/`PIPE` stdin are supported in v1. Other isolation policies and
arbitrary file descriptors are rejected before launch. Runs are headless and machine-controlled
despite being visible. Completed windows and transport resources close by default; there is no
hold-open option in v1. Cancellation is sent through the private socket, and abandoning the
owner cleans the worker, socket, and temporary directory where interpreter shutdown permits.

Execution host and isolation policy remain separate axes, avoiding one host class per host/policy
combination. Optional hosts fail closed when a requested isolation policy cannot be transported.

## Failure handling

`execute()` raises `AgentExecutionError` instead of returning when a run failed — an `error`
event was emitted, the terminal `result` had `content == "error"`, or no terminal `result`
arrived at all. `str(e)` is the bare reason; the exception also carries whatever partial
`response`/`cost`/`session_id`/`duration`/`output_tokens` the run produced before failing. When
the CLI process itself exits unsuccessfully, `returncode` is also populated; signal termination
uses Python's negative-returncode convention and supplies the positive signal number separately.

```python
from agent_shell.models.agent import AgentExecutionError

try:
    response = await shell.execute(cwd="/path/to/project", prompt="Fix the failing test")
except AgentExecutionError as e:
    print(f"run failed: {e}")   # e.g. "500 model name=qwen3.6-27b-8Q failed to load"
    print(e.returncode, e.signal)  # e.g. -15, 15 for SIGTERM; otherwise None when unavailable
```

## Stream

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

async for event in shell.stream(
    cwd="/path/to/project",
    prompt="Refactor the auth module",
    allowed_tools=["Read", "Edit", "Bash"],
    model="sonnet",
    effort="high",
    include_thinking=True,
):
    if event.type == "system":
        print(f"Session: {event.session_id}")
    else:
        print(f"[{event.type}] {event.content}")
```

## Model discovery

Ask the selected CLI which model strings it currently advertises, then pass one back
unchanged. Discovery sends no inference prompt and has no model-token cost.

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

models = await shell.list_models(cwd="/path/to/project")
selected_model = models[0]

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Review this project",
    model=selected_model,
)
```

"Available" means advertised as selectable for the current harness, account, and workspace.
It does not prove quota, entitlement, credentials, or provider health. The harness's order and
aliases such as `auto` and `default` are preserved. A genuine empty catalog returns `[]`;
discovery failures are raised instead of being mistaken for an empty catalog.

See the
[agent parameter comparison](development/agent_parameter_comparison.md#model-discovery)
for each harness's underlying discovery mechanism.

## Health check

Verify an agent + model combination actually works before relying on it. It sends a
trivial prompt and reports whether a real response came back — catching bad model names,
missing credentials, and billing/quota failures. Exit codes alone are unreliable (some
CLIs exit 0 on failure), so the verdict is read from the normalized event stream.

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

result = await shell.health_check(cwd="/path/to/project", model="haiku")
if not result.healthy:
    print(f"unavailable: {result.exception}")
```

## Restricting tools (`disallowed_tools`)

Pass a deny-list of tools that the agent must not use. Use the canonical vocabulary
`{bash, edit, read, web_search, web_fetch}` and Agent Shell translates it to each CLI's
own tool names — callers don't need to know the per-harness vocabulary:

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Audit this code but don't run anything or touch the network",
    disallowed_tools=["bash", "web_search", "web_fetch"],
)
```

- `edit` covers write/edit/notebook-edit (it fans out on harnesses that split them).
- Any name outside the canonical set passes through **verbatim** (e.g. an MCP tool
  `mcp__server__tool`, or a harness-specific name like `Write`, or Copilot's `view`).
- Deny takes precedence over auto-approve on every backend that supports it.
- Where a backend cannot enforce a deny, the adapter emits a `UserWarning` listing the
  ignored tools rather than failing silently. Coverage varies: Claude, OpenCode, and Grok
  enforce all five canonical names; Copilot enforces only `bash`/`edit` canonically (use a
  verbatim name for its other tools); Codex can only deny `web_search`; Cursor cannot
  enforce any per-call deny (its tool policy lives in `.cursor/cli.json`).
- Denying `edit` or `read` is **best-effort**: a model can still modify or read files through
  the shell, so also deny `bash` when you need a hard file boundary.

## OpenCode

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.OPENCODE)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Can you tell me about this project?",
    model="anthropic/claude-sonnet-4-5",
)

print(response.response)
print(f"Session: {response.session_id}")

# Resume the conversation using the session_id
follow_up = await shell.execute(
    cwd="/path/to/project",
    prompt="Now refactor the auth module based on your findings",
    model="anthropic/claude-sonnet-4-5",
    session_id=response.session_id,
)
```

> **Note:** For OpenCode, `allowed_tools` and `effort` are **ignored** — the adapter maps
> neither to a CLI flag nor to `opencode.json`. To restrict an OpenCode agent, use
> `disallowed_tools` (see [Restricting tools](#restricting-tools-disallowed_tools)): it is
> enforced via a per-run `OPENCODE_PERMISSION` environment variable and holds even under
> auto-approve. Keep `auto_approve=True` (the default) — with `auto_approve=False`,
> `opencode run` auto-*rejects* permission prompts non-interactively and can silently abort the run.

## Cursor

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.CURSOR)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Can you tell me about this project?",
)

print(response.response)
print(f"Session: {response.session_id}")
```

> **Note:** Cursor runs headlessly via `cursor-agent --print --output-format stream-json` and
> **requires** workspace trust, which the adapter always passes (`--trust`). With
> `auto_approve=True` (the default) it also passes `--force` so tools auto-run; otherwise
> tools are auto-*rejected* but the run still completes. `allowed_tools`, `effort`, and
> `disallowed_tools` are **ignored** — Cursor exposes no per-call tool policy or effort flag
> (tool policy lives in `.cursor/cli.json`), so each emits a `UserWarning`. On a Free plan
> only `model=None`/`"auto"` works. MCP add/remove/list are supported by directly managing
> the user-scope `~/.cursor/mcp.json` file because `cursor-agent mcp` has no add/remove
> subcommands.

## Grok

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

shell = AgentShell(agent_type=AgentType.GROK)

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Can you tell me about this project?",
    model="grok-4.5",
)

print(response.response)
print(f"Session: {response.session_id}")
print(f"Cost: ${response.cost:.4f}")
```

> **Note:** Grok runs headlessly via `grok -p --output-format streaming-messages-json`
> (full assistant blocks — not token-delta `streaming-json`, which would break
> newline-joined text collection). With `auto_approve=True` (the default) the adapter
> passes `--always-approve`. `effort` maps to `--reasoning-effort`, `allowed_tools` to
> `--tools`, and `disallowed_tools` to `--disallowed-tools` (canonical `bash` maps to
> `run_terminal_cmd` — the working deny id, not init.tools' `run_terminal_command`).
> The terminal `result` event carries cost (may be `0` on some auth paths), duration, and
> raw `usage.output_tokens` (reasoning is already inside that figure when reported).
> MCP add/remove/list are supported via `grok mcp` with **user scope only**
> (`~/.grok/config.toml`).

## MCP Servers

Register MCP servers for any supported agent through a unified API. All adapters use user-scope
configuration so registrations persist across the agent's `execute`/`stream` calls.

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType

shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

# Register a stdio MCP server (e.g. forgetful) before running an eval
await shell.add_mcp_server(MCPServerSpec(
    name="forgetful",
    type=MCPServerType.STDIO,
    command="uvx",
    args=["forgetful-ai"],
    env={"FORGETFUL_API_KEY": "..."},
))

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Recall any prior decisions about the auth module",
)

# Optional cleanup
await shell.remove_mcp_server("forgetful")
```

For HTTP transport, pass `url` and `headers` instead of `command`/`args`/`env`:

```python
await shell.add_mcp_server(MCPServerSpec(
    name="remote",
    type=MCPServerType.HTTP,
    url="https://example.com/mcp",
    headers={"Authorization": "Bearer ..."},
))
```

`add_mcp_server` adds or updates a server with the same name. Cursor preserves native fields
that `MCPServerSpec` cannot represent when an update keeps the same transport. The configuration
is written atomically with user-only permissions. `remove_mcp_server` warns rather than raises
when the named server is not found. `list_mcp_servers()` works for
Claude Code, OpenCode, Copilot CLI, Codex, Cursor, and Grok. Claude Code reads user-scope
entries directly from `~/.claude.json`, Cursor from `~/.cursor/mcp.json`, and Grok from
`~/.grok/config.toml`, so listing does not launch configured servers for health checks. MCP
is not supported for Pi; all three MCP methods raise `NotImplementedError`.

## Logging

Agent Shell uses Python's standard `logging` module. Configure the `agent_shell` logger to capture
tool calls, session IDs, costs, and errors:

```python
import logging

logging.getLogger("agent_shell").setLevel(logging.INFO)
logging.getLogger("agent_shell").addHandler(logging.StreamHandler())
```

Set to `DEBUG` for raw JSON events and full command arguments.
