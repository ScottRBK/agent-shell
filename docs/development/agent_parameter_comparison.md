# Agent CLI Parameter Comparison

Comparison of headless/non-interactive configuration across supported CLI coding agents.
Last updated: 2026-03-29

## Summary Table

| Capability | Claude Code | Gemini CLI | Codex | Copilot CLI | OpenCode |
|---|---|---|---|---|---|
| **Headless flag** | `-p` | `-p` | `exec` subcommand | `-p` | `run` subcommand |
| **Model** | `--model` | `-m` | `--model` / `-m` | `--model` | `--model` / `-m` |
| **Effort/Thinking** | `--effort` (low/med/high/max) | `thinkingConfig` in settings.json | `-c model_reasoning_effort=` | `--effort` / `--reasoning-effort` | `reasoningEffort` in config |
| **Allowed tools** | `--allowed-tools` | `tools` in settings.json | No direct flag | `--allow-tool`, `--available-tools` | `tools` in config |
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

## Unified Interface Recommendations

Based on this comparison, the following parameters map across all agents:

### Universal (all agents support)
- `prompt` - the task/question
- `cwd` - working directory
- `model` - model selection

### Widely supported (most agents, mechanism varies)
- `allowed_tools` - tool filtering (CLI flags for Claude/Copilot, config for others)
- `effort` - reasoning effort level (direct flag or config-based)
- `include_thinking` - whether to surface reasoning (adapter controls how)

### Partially supported (some agents only)
- `system_prompt` - only Claude Code has a direct flag, others use files/env vars
- `max_budget` - only Claude Code supports this directly
- `max_turns` - only Claude Code supports this directly

Adapters should translate common parameters into whatever mechanism each agent requires (flags, config files, env vars).
