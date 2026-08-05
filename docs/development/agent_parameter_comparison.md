# Agent CLI Parameter Comparison

Comparison of headless/non-interactive configuration across supported CLI coding agents.
Last updated: 2026-08-05

> The summary matrix below has no Pi or Cursor column (it predates both adapters); see the
> per-agent detail sections, the model-discovery section, and the `disallowed_tools` table.

> Every "measured 2026-07-26" claim below comes from a real three-call run per agent (first
> turn, resume on the returned id, then a session-less turn), recorded by the resume e2e tests
> in `tests/e2e/test_*_e2e.py`.

## Summary Table

| Capability | Claude Code | Codex | Copilot CLI | OpenCode |
|---|---|---|---|---|
| **Headless flag** | `-p` | `exec` subcommand | `-p` | `run` subcommand |
| **Model** | `--model` | `--model` / `-m` | `--model` | `--model` / `-m` |
| **Effort/Thinking** | `--effort` (low/med/high/max) | `-c model_reasoning_effort=` | `--effort` / `--reasoning-effort` | `reasoningEffort` in config |
| **Allowed tools** | `--allowed-tools` | No direct flag | `--allow-tool`, `--available-tools` | `tools` in config |
| **Disallowed tools** | `--disallowed-tools` | `web_search` config only | `--deny-tool` | `OPENCODE_PERMISSION` env / `permission` config |
| **Stream output** | `--output-format stream-json` | `--json` (NDJSON) | `--output-format=json` (JSONL) | `--format json` |
| **Working dir** | cwd + `--add-dir` | `--cd` / `-C` | cwd / `-C` | cwd |
| **System prompt** | `--system-prompt` / `--append-system-prompt` | No flag (files only) | No flag (files only) | `instructions` in config |
| **Budget** | `--max-budget-usd` | No direct flag | `--max-ai-credits` | No direct flag |
| **Auto-approve** | `--dangerously-skip-permissions` | `--yolo` | `--yolo` / `--allow-all` | Auto in `run` mode |
| **Session resume** | `--resume` | `exec resume <id>` | `--resume` | `-s <id>` |

## Model discovery

Every adapter exposes the same account/workspace-aware API:

```python
shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

models = await shell.list_models(cwd="/path/to/project")
selected_model = models[0]

response = await shell.execute(
    cwd="/path/to/project",
    prompt="Do the work",
    model=selected_model,
)
```

`list_models()` returns `list[str]`. Each string is the exact selector accepted by that
same adapter's `execute(model=...)` and `stream(model=...)`; callers do not translate it.
Discovery reads CLI metadata and makes no inference call.

| Harness | Discovery mechanism | Returned selector |
|---|---|---|
| Claude Code | stream-JSON `initialize` control request | `models[].value` |
| OpenCode | `opencode models` | each complete output line |
| Copilot CLI | headless JSON-RPC `models.list` | `models[].id` |
| Codex | `codex debug models` | visible model `slug` |
| Pi | `pi --no-approve --list-models` | `provider/model` |
| Cursor | `cursor-agent models` | ID before the first ` - ` |

Important semantics:

- "Available" means advertised as selectable for the current harness, account, and
  workspace. It does not guarantee current quota, entitlement, credentials, or provider
  health. Use `health_check(model=...)` when actual execution must be proven.
- Claude and Copilot are invoked directly as CLIs. Their SDK documentation was protocol
  evidence only; AgentShell imports no harness SDKs and adds no runtime dependency.
- Claude runs in print mode with `--no-session-persistence`, so discovery is not saved as a
  resumable session.
- Codex filters the refreshed JSON catalog to `visibility == "list"`. Its documented
  `debug models` command is experimental, so integration and local E2E tests guard drift.
- Pi model IDs are provider-qualified because IDs can repeat across providers. Discovery
  does not run the mutating `pi update --models` command.
- The harness's order and aliases such as `auto` or `default` are preserved.
- A genuine empty catalog returns `[]`. Authentication, timeout, non-zero exit, and
  malformed protocol/output failures are surfaced with actionable errors.
- AgentShell does not cache or replace results with a static fallback catalog.

The behavior was verified against all six installed CLIs on 2026-08-02 without inference
spend. CI covers captured real outputs through `AgentShell`; a local E2E test checks all six
real discovery commands.

## Claude Code

- **Headless mode**: `-p` / `--print`
- **Model**: `--model` accepts alias (`sonnet`, `opus`) or full name (`claude-sonnet-4-6`)
- **Fallback model**: `--fallback-model` for automatic fallback when primary is overloaded
- **Effort**: `--effort low|medium|high|max` (max is Opus 4.6 only)
- **Allowed tools**: `--allowed-tools` comma/space-separated with prefix matching (`Bash(git:*)`)
- **Disallowed tools**: `--disallowed-tools` removes tools from context entirely
- **Tool restriction**: `--tools` restricts which built-in tools are available
- **Output format**: `--output-format text|json|stream-json` (stream-json requires `--verbose`)
- **Partial messages**: `--include-partial-messages` for token-level streaming
- **Working directory**: Uses cwd, `--add-dir` for additional directories
- **System prompt**: `--system-prompt` (replaces default), `--append-system-prompt` (adds to default), file variants available
- **Budget**: `--max-budget-usd` maximum dollar spend
- **Max turns**: `--max-turns` limit agentic turns
- **Auto-approve**: `--dangerously-skip-permissions` or `--permission-mode`
- **JSON schema**: `--json-schema` for structured output validation
- **Session**: `--continue`, `--resume`, `--session-id`, `--no-session-persistence`. The adapter
  resumes with `--resume <id>`. The resumed run reports the SAME `session_id` in its json stream,
  and an unknown id is rejected (the run errors), so id identity is real evidence the CLI
  continued that session (measured 2026-07-26)
- **Startup**: `--bare` skips all auto-discovery (hooks, MCP, CLAUDE.md, plugins)

## Codex (OpenAI)

- **Headless mode**: `codex exec` subcommand (streams progress to stderr, final message to stdout)
- **Model**: `--model` / `-m` (e.g. `gpt-5.4`, `gpt-5-codex`)
- **Effort**: Config only via `model_reasoning_effort` (low/medium/high), passable as `-c model_reasoning_effort='"high"'`
- **Allowed tools**: No direct CLI flag for tool filtering
- **Approval mode**: `--ask-for-approval untrusted|on-request|never`, `--full-auto`, `--yolo`
- **Sandbox**: `--sandbox read-only|workspace-write|danger-full-access`
- **Output format**: `--json` for NDJSON events to stdout
- **Working directory**: `--cd` / `-C`
- **System prompt**: No CLI flag, uses `AGENTS.md` files in repo
- **Budget**: No per-run flag, `model_context_window` in config
- **Output schema**: `--output-schema` for structured JSON output
- **Session**: `--continue`, `--session`, `--ephemeral` (don't persist). The adapter resumes with
  the `codex exec resume <id>` SUBCOMMAND — the id is positional, not a flag. The resumed run
  reports the SAME `thread_id`, and an unknown id is rejected ("no rollout found for thread id"),
  so id identity is real evidence the CLI continued that thread (measured 2026-07-26)
- **Config**: `~/.codex/config.toml`, project `.codex/config.toml`, `-c key=value` overrides
- **Profiles**: `--profile` to load named config profiles

## Copilot CLI (GitHub)

- **Headless mode**: `-p` / `--prompt` for one-shot; `--acp` for Agent Client Protocol
- **Model**: `--model <model>`; use `auto` to let Copilot choose
- **Effort**: `--effort` / `--reasoning-effort` with choices
  `none|minimal|low|medium|high|xhigh|max`. AgentShell accepts effort values
  case-insensitively, validates them against these choices, and passes the normalized lowercase
  value to Copilot via `--effort`.
- **Allowed tools**: `--allow-tool`, `--deny-tool`, `--available-tools`,
  `--excluded-tools`, `--allow-all-tools`
- **Auto-approve**: `--allow-all-tools`; `--allow-all` / `--yolo` also grant path and URL
  permissions
- **Agent mode**: `--mode interactive|plan|autopilot`; `--autopilot` is the shortcut
- **Output format**: `--output-format=json` (JSONL)
- **Silent mode**: `--silent` suppresses stats, prints only response
- **Working directory**: CLI supports `-C <directory>`; AgentShell sets subprocess `cwd`
- **System prompt**: No CLI flag; uses `.github/copilot-instructions.md` and `AGENTS.md` files
- **AI credit budget**: `--max-ai-credits <credits>` limits credits for the session
- **Token budget**: No per-run flag; auto-compacts at 95% of the token limit
- **Path permissions**: `--allow-all-paths`, `--disallow-temp-dir`
- **URL permissions**: `--allow-all-urls`, `--allow-url`, `--deny-url`
- **Session**: `--resume`, `--continue`, and `--session-id <id>`. The latter resumes an existing
  session or task, or assigns a UUID to a new session. AgentShell resumes with `--resume <id>`.
  The resumed run reports the SAME `sessionId`, and an unknown id is rejected
  ("No session, task, or name matched"), so id identity is real evidence the CLI continued that
  session (measured 2026-07-26).
- **ACP mode**: `--acp` uses stdio by default. Copilot CLI 1.0.78 also accepts hidden
  `--stdio` and `--port <port>` transport options; they are not shown by `copilot --help`.

## OpenCode

- **Headless mode**: `opencode run` subcommand (auto-approves all tools)
- **Model**: `--model` / `-m` as `provider/model` (e.g. `anthropic/claude-sonnet-4-5`)
- **Small model**: Separate `small_model` config for lightweight tasks
- **Effort**: `reasoningEffort` in config (low/medium/high/xhigh), per-agent overrides
- **Allowed tools**: `tools` object in `opencode.json` config
- **Permissions**: `permission` config or `OPENCODE_PERMISSION` env var
- **Output format**: `--format default|json` (json = streaming JSON events)
- **Working directory**: Uses cwd (no flag for `run` mode)
- **System prompt**: `instructions` array in config pointing to file paths/globs
- **Token management**: `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`, `compaction` config
- **Session**: `--continue`, `--session`, `--fork`. The adapter resumes with `-s <id>`. The
  resumed run reports the SAME `sessionID`, and an unknown id fails the run (no result event),
  so id identity is real evidence the CLI continued that session (measured 2026-07-26)
- **Server mode**: `opencode serve --port 4096` with `--attach` from `run`
- **Config**: `opencode.json` at project or `~/.config/opencode/opencode.json`, `OPENCODE_CONFIG_CONTENT` env var

## Cursor

- **Headless mode**: `-p` / `--print` with `--output-format stream-json` (NDJSON)
- **Model**: `--model` (e.g. `sonnet-4-thinking`, `gpt-5`); parameterized models take bracket
  overrides, e.g. `claude-opus-4-8[context=1m,effort=high,fast=false]`. On a Free plan only
  `auto` is available.
- **Effort**: No standalone flag — only the model bracket-override above, which the adapter
  does not inject, so `effort` is ignored and warns
- **Allowed tools**: No per-call flag — tool policy lives in `.cursor/cli.json`, so
  `allowed_tools` is ignored and warns
- **Disallowed tools**: No per-call flag (same reason) — `disallowed_tools` is ignored and warns
- **Workspace trust**: `--trust` is MANDATORY headlessly (an untrusted dir exits 1 with a
  plain-text "Workspace Trust Required" on stderr and zero stdout); the adapter always passes it
- **Auto-approve**: `-f` / `--force` (alias `--yolo`) auto-runs tools; without it tools
  auto-*reject* but the run still completes (exit 0)
- **Output format**: `--output-format text|json|stream-json` (only with `--print`);
  `--stream-partial-output` duplicates text and is not used
- **Working directory**: uses cwd, `--add-dir` for extra roots, `-w` / `--worktree` for isolation
- **Session**: `--resume [chatId]` (the adapter uses the `--resume=<id>` form), `--continue`,
  `create-chat`. The resumed run reports the SAME `session_id`. Unlike the four above,
  cursor-agent ACCEPTS an unknown id — `--resume=<never-seen-uuid>` starts a session under that
  id and echoes it back rather than failing, so id identity proves the flag was passed through
  and honoured, not that a prior transcript was replayed (measured 2026-07-26)
- **MCP**: `cursor-agent mcp` = login/list/list-tools/enable/disable only (no add/remove);
  servers declared in `.cursor/mcp.json`. All three adapter MCP methods raise
  `NotImplementedError` — including `list_mcp_servers`, because `mcp list` emits only
  `name: status` and cannot rebuild an `MCPServerSpec`
- **Usage**: the terminal `result` event carries `usage.outputTokens` (undocumented but real)
  and `duration_ms`; there is no cost field, so `cost` is `0.0`

## Pi

Flags below are the ones the adapter actually emits (`_build_command` in
`src/agent_shell/adapters/pi_adapter.py`); this is not a full survey of the Pi CLI.

- **Headless mode**: `--print` combined with `--mode json` (NDJSON events on stdout)
- **Model**: `--model` as `provider/model` (e.g. `openai-codex/gpt-5.4-mini`)
- **Effort**: no separate effort flag — `--thinking` IS the reasoning knob, and its levels
  (off/minimal/low/medium/high/xhigh) match the `effort` vocabulary
- **Allowed tools**: `--tools` (comma-joined)
- **Disallowed tools**: `--exclude-tools` (comma-joined)
- **Trust**: a decision MUST be passed explicitly — with neither `--approve` nor `--no-approve`,
  `pi -p` blocks on an interactive "trust project?" prompt and never returns
- **Session**: the adapter resumes with `--session-id <id>`. The resumed run reports the SAME
  session `id`. Unlike Claude Code/Codex/Copilot/OpenCode, `--session-id` is an UPSERT — an
  unknown id is accepted and echoed back rather than rejected, so id identity proves the flag
  was passed through and honoured, not that a prior transcript was continued. It IS continued:
  both turns land in one `~/.pi/agent/sessions/<cwd>/<ts>_<id>.jsonl` (measured 2026-07-26)
- **Usage**: each `agent_end` carries only the messages THIS agent loop produced, so summing
  their usage bills the caller for their own turn and nothing else; a single run can emit
  several `agent_end` events (retry, auto-compaction)
- **Exit code**: pi exits 0 even on a model error — failure is detected from the last assistant
  message's `stopReason` (`error`/`aborted`), not from the process return code

## Unified Interface Recommendations

Based on this comparison, the following parameters map across all agents:

### Universal (all agents support)
- `prompt` - the task/question
- `cwd` - working directory
- `model` - model selection

### Widely supported (most agents, mechanism varies)
- `allowed_tools` - tool filtering (CLI flags for Claude/Copilot, config for others)
- `disallowed_tools` - tool deny-list (see below)
- `effort` - reasoning effort level (direct flag or config-based)
- `include_thinking` - whether to surface reasoning (adapter controls how)

#### `disallowed_tools` — canonical deny vocabulary

Callers pass canonical names so they need not know each CLI's tool vocabulary. The
canonical set is `{bash, edit, read, web_search, web_fetch}` (write/edit/patch collapse
into one `edit`). Each adapter owns a `canonical -> [native]` map (`tool_denial.py`),
fanning out where a harness splits the concept. Names outside the canonical set pass
through **verbatim** (e.g. `mcp__server__tool`, or a harness-specific name like `Write`).

| Adapter | Mechanism | Notes |
|---|---|---|
| Claude Code | `--disallowed-tools` (comma-joined) | `edit` → `Edit,Write,NotebookEdit`; precedence over skip-permissions |
| Copilot CLI | repeated `--deny-tool` | only `bash`→`shell` and `edit`→`write` are canonically mapped (the CLI's confirmed permission names); `read`/`web_search`/`web_fetch` **warn** — Copilot has no web tools and silently ignores unknown deny names. Deny rules take precedence over `--allow-all-tools` |
| OpenCode | `OPENCODE_PERMISSION` env var, process-scoped | merges over any inherited value (deny wins), fails closed on bad JSON; hard block before approval flow |
| Codex | `-c web_search="disabled"` only | no name-based deny; web_search key verified on codex-cli 0.133.0 but version-fragile (upstream `web_search_mode`), guarded by an e2e test; every other canonical/verbatim name warns and is ignored |
| Cursor | none (no per-call flag) | tool policy is config-file only (`.cursor/cli.json`); the adapter has no `canonical → native` map, so **every** deny (canonical or verbatim) warns and is ignored |
| Pi | `--exclude-tools` (comma-joined) | `edit` → `edit,write`; no web tool, so those warn |

When an adapter cannot honor a requested canonical deny it emits a `UserWarning` listing
the ignored names rather than silently dropping the deny (fail-loud). A caller who knows a
backend's exact native tool name can always pass it **verbatim** (it bypasses the canonical
map), e.g. `disallowed_tools=["view"]` on Copilot.

### Partially supported (some agents only)
- `system_prompt` - only Claude Code has a direct flag, others use files/env vars
- `max_budget` - only Claude Code supports this directly
- `max_turns` - only Claude Code supports this directly

Adapters should translate common parameters into whatever mechanism each agent requires (flags, config files, env vars).
