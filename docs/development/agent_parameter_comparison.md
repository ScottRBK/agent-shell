# Agent CLI Parameter Comparison

Comparison of headless/non-interactive configuration across supported CLI coding agents.
Last updated: 2026-07-10

> The summary matrix below predates the Pi and Cursor adapters; see their per-agent detail
> sections (and the `disallowed_tools` table) for those two.

## Summary Table

| Capability | Claude Code | Gemini CLI | Codex | Copilot CLI | OpenCode |
|---|---|---|---|---|---|
| **Headless flag** | `-p` | `-p` | `exec` subcommand | `-p` | `run` subcommand |
| **Model** | `--model` | `-m` | `--model` / `-m` | `--model` | `--model` / `-m` |
| **Effort/Thinking** | `--effort` (low/med/high/max) | `thinkingConfig` in settings.json | `-c model_reasoning_effort=` | `--effort` / `--reasoning-effort` | `reasoningEffort` in config |
| **Allowed tools** | `--allowed-tools` | `tools` in settings.json | No direct flag | `--allow-tool`, `--available-tools` | `tools` in config |
| **Disallowed tools** | `--disallowed-tools` | `tools.exclude` in settings.json | `web_search` config only | `--deny-tool` | `OPENCODE_PERMISSION` env / `permission` config |
| **Stream output** | `--output-format stream-json` | `-o stream-json` | `--json` (NDJSON) | `--output-format=json` (JSONL) | `--format json` |
| **Working dir** | cwd + `--add-dir` | cwd + `--worktree` | `--cd` / `-C` | cwd (no flag) | cwd (no flag) |
| **System prompt** | `--system-prompt` / `--append-system-prompt` | `GEMINI_SYSTEM_MD` env var | No flag (files only) | No flag (files only) | `instructions` in config |
| **Budget** | `--max-budget-usd` | No direct flag | No direct flag | No direct flag | No direct flag |
| **Auto-approve** | `--dangerously-skip-permissions` | `--approval-mode yolo` | `--yolo` | `--yolo` / `--allow-all` | Auto in `run` mode |

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
- **Session**: `--continue`, `--resume`, `--session-id`, `--no-session-persistence`
- **Startup**: `--bare` skips all auto-discovery (hooks, MCP, CLAUDE.md, plugins)

## Gemini CLI

- **Headless mode**: `-p` / `--prompt` (also auto-activates in non-TTY environments)
- **Model**: `-m` / `--model` accepts model name (e.g. `gemini-2.5-pro`)
- **Model (env)**: `GEMINI_MODEL` env var or `settings.json`
- **Effort**: `thinkingConfig` in `settings.json` with `thinkingBudget` and `thinkingLevel` (OFF/BASIC/MODERATE/HIGH)
- **Allowed tools**: `tools` object in `settings.json` with `allowed`, `core`, `exclude` arrays
- **Approval mode**: `--approval-mode default|auto_edit|yolo`
- **Output format**: `-o` / `--output-format text|json|stream-json`
- **Working directory**: Uses cwd, `--worktree` for git worktrees, `--include-directories` for extras
- **System prompt**: `GEMINI_SYSTEM_MD` env var pointing to file path (full replacement)
- **Token management**: `model.maxSessionTurns`, `model.compressionThreshold`, `maxOutputTokens` in config
- **Sandbox**: `-s` / `--sandbox` for sandboxed tool execution
- **Session**: `--resume` to continue previous session
- **Exit codes**: 0 success, 1 error, 42 input error, 53 turn limit exceeded

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
- **Session**: `--continue`, `--session`, `--ephemeral` (don't persist)
- **Config**: `~/.codex/config.toml`, project `.codex/config.toml`, `-c key=value` overrides
- **Profiles**: `--profile` to load named config profiles

## Copilot CLI (GitHub)

- **Headless mode**: `-p` / `--prompt` for one-shot, `--acp --stdio` for programmatic JSON-RPC
- **Model**: `--model` (default `claude-sonnet-4.5`)
- **Effort**: `--effort low|medium|high` or `--reasoning-effort low|medium|high`
- **Allowed tools**: `--allow-tool`, `--deny-tool`, `--available-tools`, `--excluded-tools`, `--allow-all-tools`
- **Auto-approve**: `--allow-all` / `--yolo`, `--autopilot`
- **Output format**: `--output-format=json` (JSONL)
- **Silent mode**: `--silent` suppresses stats, prints only response
- **Working directory**: Uses cwd (no flag), ACP mode uses `newSession` parameter
- **System prompt**: No CLI flag, uses `.github/copilot-instructions.md` and `AGENTS.md` files
- **Budget**: No per-run flag, auto-compacts at 95% token limit
- **Path permissions**: `--allow-all-paths`, `--disallow-temp-dir`
- **URL permissions**: `--allow-all-urls`, `--allow-url`, `--deny-url`
- **Session**: `--resume`, `--continue`
- **ACP mode**: `--acp --stdio` or `--acp --port 3000` for JSON-RPC integration

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
- **Session**: `--continue`, `--session`, `--fork`
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
  `create-chat`
- **MCP**: `cursor-agent mcp` = login/list/list-tools/enable/disable only (no add/remove);
  servers declared in `.cursor/mcp.json`
- **Usage**: the terminal `result` event carries `usage.outputTokens` (undocumented but real)
  and `duration_ms`; there is no cost field, so `cost` is `0.0`

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

When an adapter cannot honor a requested canonical deny it emits a `UserWarning` listing
the ignored names rather than silently dropping the deny (fail-loud). A caller who knows a
backend's exact native tool name can always pass it **verbatim** (it bypasses the canonical
map), e.g. `disallowed_tools=["view"]` on Copilot.

### Partially supported (some agents only)
- `system_prompt` - only Claude Code has a direct flag, others use files/env vars
- `max_budget` - only Claude Code supports this directly
- `max_turns` - only Claude Code supports this directly

Adapters should translate common parameters into whatever mechanism each agent requires (flags, config files, env vars).
