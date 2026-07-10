# Implementation Plan: `disallowed_tools` (tool deny-list)

> Status: implemented (2026-06-16). Shared helper `adapters/tool_denial.py`; wired through
> `shell.py` and all four adapters; unit + integration tests green (`uv run pytest`).
> Parameter name in code: **`disallowed_tools: list[str] | None = None`** (this file is
> named `disabled_tools.md` per request; the canonical identifier is `disallowed_tools`).

> **Later adapters (this plan predates them).** Two more adapters were added after this
> document and follow the same fail-loud model:
> - **Pi** (2026-06-27): uses `resolve_disallowed_tools` with `bash`/`edit`(→`edit,write`)/`read`
>   mapped to `--exclude-tools`; `web_search`/`web_fetch` are unenforceable and **warn** (Pi
>   ships no web tool). See `pi_adapter.py`.
> - **Cursor** (2026-07-10): has **no** per-call deny mechanism at all (tool policy lives in
>   `.cursor/cli.json`), so it does **not** use `resolve_disallowed_tools` — like Codex — and
>   **every** requested deny warns and is ignored. Its `allowed_tools` and `effort` are likewise
>   unsupported (warn-once); `include_thinking` is honoured. See `cursor_adapter.py`.

## 1. Goal

Let a caller pass a deny-list of tools to `AgentShell` and have each adapter prevent the
underlying CLI from using those tools — **without the caller needing to know each
harness's tool vocabulary**. A single call such as:

```python
shell = AgentShell(AgentType.CODEX)      # or CLAUDE_CODE / OPENCODE / COPILOT_CLI
await shell.execute(cwd=".", prompt="...", disallowed_tools=["bash", "web_search"])
```

must translate correctly to every backend, or warn loudly where it cannot.

## 2. Why a canonical vocabulary (not passthrough)

The same concept has a different identifier in every CLI:

| Concept | Claude Code | Copilot | OpenCode | Codex |
|---|---|---|---|---|
| shell exec | `Bash` | `shell` | `bash` | — (no name-based deny) |
| modify files | `Write` / `Edit` / `NotebookEdit` | `write` / `edit` | `edit` (collapses write/edit/patch) | — |
| read files | `Read` | `read` | `read` | — |
| web search | `WebSearch` | `web_search` | `websearch` | config: `web_search=disabled` |
| web fetch | `WebFetch` | `web_fetch` | `webfetch` | — |

Pure passthrough would mean `disallowed_tools=["websearch"]` silently denies nothing on
Claude/Copilot/Codex — a deny that *looks* applied but isn't (the worst failure mode for
a security control). So `agent_shell` owns a small **canonical vocabulary** and each
adapter translates it, exactly as it already does for every other unified parameter and
for `MCPServerSpec`.

## 3. Design decisions (locked)

1. **Canonical set** (security-relevant core, snake_case):
   `{"bash", "edit", "read", "web_search", "web_fetch"}`.
   `write` and `edit` are **merged into a single `edit`** canonical (following OpenCode's
   premise that write/edit/patch are one permission). On harnesses that split the concept
   (Claude, Copilot) the one canonical name **fans out** to all native modify tools.
2. **Per-adapter translation.** Each adapter owns a `dict[canonical -> list[native]]`.
   Translation lives in the adapter, consistent with the existing pattern.
3. **Passthrough escape hatch.** Any name *not* in the canonical set is passed through
   **verbatim** to the harness (MCP tools like `mcp__server__tool`, or harness-specific
   names like `Write` to deny *only* write on Claude). Verbatim names are inherently
   harness-specific — that is the caller's choice.
4. **Fail-loud on enforcement gaps.** When an adapter cannot honor a requested canonical
   deny (e.g. Codex asked to deny `bash`), it emits `warnings.warn(UserWarning, ...)`
   listing the ignored tools, then proceeds. Never silently drop a requested deny.
5. **Safe with `auto_approve`.** Confirmed via source **and empirically** (opencode 1.14.41):
   on every backend that has a deny mechanism, **deny beats allow-all / skip-permissions**, so
   combining the deny-list with `auto_approve` is safe — the deny wins. For OpenCode this is
   load-bearing: a `deny` rule raises `DeniedError` in `permission/index.ts` *before* any
   `permission.asked` event is published, and `--dangerously-skip-permissions` only auto-approves
   `ask` events — so it never sees a denied tool. Verified by a real run: `{"bash":"deny"}` (and
   the wildcard `{"*":"deny"}`) block the shell tool even with the flag set; the bare string
   `"deny"` does **not** (see the bare-string note below). OpenCode's `auto_approve` is now wired
   to `--dangerously-skip-permissions` (upstream fix, integrated here) — the prior "accepted but
   never mapped" drift is resolved, and the deny holds alongside it. The e2e guard
   `test_bash_deny_blocks_shell_under_skip_permissions` re-checks this on every opencode upgrade.
6. **`read` is best-effort, not a confidentiality boundary.** Denying `read` blocks the
   dedicated read tool only. File contents can still be reached via `grep`/`glob`/`lsp`
   and via `bash` (`cat`, etc.). To actually wall off file contents, a caller must also
   deny `bash` (and accept that search tools may still match). Documented, not solved here.
7. **`edit` deny is not write-confinement while `bash` is allowed.** Denying `edit` blocks the
   dedicated write/edit tools, but a model can still mutate files through shell (`bash`/`shell`)
   — and on Copilot `write` explicitly excludes shell. To actually prevent file modification, a
   caller must deny **both** `edit` and `bash`. Same class of caveat as decision 6.

## 4. Capability + mapping matrix

| Canonical | Claude `--disallowed-tools` | Copilot `--deny-tool` | OpenCode `OPENCODE_PERMISSION` | Codex |
|---|---|---|---|---|
| `bash` | `Bash` | `shell` | `bash` | ⚠️ warn |
| `edit` | `Edit`,`Write`,`NotebookEdit` | `write` | `edit` | ⚠️ warn |
| `read` | `Read` | ⚠️ warn | `read` | ⚠️ warn |
| `web_search` | `WebSearch` | ⚠️ warn | `websearch` | `-c web_search="disabled"` ✅ |
| `web_fetch` | `WebFetch` | ⚠️ warn | `webfetch` | ⚠️ warn |
| *unknown name* | verbatim | verbatim | verbatim (as a deny key) | ⚠️ warn |

Codex is the only adapter that cannot deny by tool name; it can disable web search via a
config override only. Everything else on Codex → warn-and-ignore (matching the existing
`allowed_tools` precedent at `codex_adapter.py:69`).

## 5. New shared module: `src/agent_shell/adapters/tool_denial.py`

A single helper used by Claude, Copilot, and OpenCode (Codex is special-cased — see §7).

```python
"""Canonical tool-name vocabulary for disallowed_tools and per-adapter translation."""

# Security-relevant core. `write`/`edit`/`patch` are intentionally one concept: "edit".
CANONICAL_TOOLS = frozenset({"bash", "edit", "read", "web_search", "web_fetch"})


def resolve_disallowed_tools(
    disallowed_tools: list[str] | None,
    native_map: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    """Translate canonical deny-names into one adapter's native tool identifiers.

    Returns (native, unsupported):
      - native:      deduped native identifiers to deny on this adapter. Canonical
                     names are mapped via native_map (and may fan out to several
                     native names); names outside CANONICAL_TOOLS pass through verbatim.
      - unsupported: canonical names this adapter cannot deny (native_map omits them);
                     the caller should warnings.warn(...) about these.
    """
    if not disallowed_tools:
        return [], []

    native: list[str] = []
    unsupported: list[str] = []
    for name in disallowed_tools:
        if name in CANONICAL_TOOLS:
            mapped = native_map.get(name)
            if mapped is None:
                unsupported.append(name)
            else:
                native.extend(mapped)
        else:
            native.append(name)  # passthrough, verbatim

    seen: set[str] = set()
    deduped = [t for t in native if not (t in seen or seen.add(t))]
    return deduped, unsupported
```

Notes:
- An adapter that supports *all* canonical names (Copilot) simply provides a complete
  `native_map`; `unsupported` will always be empty for it.
- Dedup matters for OpenCode (`edit` already collapsed) and for any caller passing
  overlapping canonical + verbatim names.

## 6. Per-file changes

### 6.1 Protocol — `agent_adapter_protocol.py` ✅ already done
`disallowed_tools` is present in both `execute()` and `stream()` (lines 10, 24). No change.

### 6.2 `shell.py` — passthrough
Add `disallowed_tools: list[str] | None = None` to `AgentShell.execute()` and
`AgentShell.stream()` (immediately after `allowed_tools`, matching protocol order) and
forward it in both `self._adapter.execute(...)` / `self._adapter.stream(...)` calls.

### 6.3 Claude Code — `claude_code_adapter.py`
- Add `disallowed_tools` to `execute()` signature + forward to `self.stream(...)`.
- Add `disallowed_tools` to `stream()` signature.
- Module-level map + translation in `stream()`:

```python
_DISALLOWED_TOOL_MAP = {
    "bash": ["Bash"],
    "edit": ["Edit", "Write", "NotebookEdit"],
    "read": ["Read"],
    "web_search": ["WebSearch"],
    "web_fetch": ["WebFetch"],
}
```
```python
# after the allowed_tools block
native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
if unsupported:
    warnings.warn(f"Claude Code cannot deny {unsupported}; ignoring", UserWarning, stacklevel=2)
if native:
    cmd.extend(["--disallowed-tools", ",".join(native)])
```
(`unsupported` is always empty for Claude today, but the warn path stays for consistency
and future-proofing.) Safe alongside `--dangerously-skip-permissions` — deny wins.

### 6.4 Copilot CLI — `copilot_cli_adapter.py`
- Same signature/forward changes.
- Map + repeated `--deny-tool`:

```python
_DISALLOWED_TOOL_MAP = {
    "bash": ["shell"],
    "edit": ["write"],
}
```
> **Corrected during review (empirical CLI inspection, copilot 1.0.40).** Only `shell` and
> `write` are confirmed `--deny-tool` permission names (`copilot --help`:
> `--allow-tool='shell(git:*)'`, `--allow-tool='write'`). The installed binary contains **no**
> `web_search`/`web_fetch` tools at all (its web tool is `fetch`), and the Copilot **SDK** tool
> vocabulary (`bash`/`edit`/`view`) differs from the **CLI flag** vocabulary (`shell`/`write`).
> Because Copilot silently ignores an unrecognized `--deny-tool` name, the original
> `read`→`read` / `web_search`→`web_search` / `web_fetch`→`web_fetch` mappings would have been
> **silent no-op denies** — the exact failure mode this feature exists to prevent. So those
> canonical names are intentionally left unmapped → the adapter warns (fail-loud). A caller who
> knows their build's exact tool name can pass it verbatim (e.g. `["view"]`, `["fetch"]`).
```python
native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
if unsupported:
    warnings.warn(f"Copilot CLI cannot deny {unsupported}; ignoring", UserWarning, stacklevel=2)
for tool in native:
    cmd.extend(["--deny-tool", tool])
```
Safe alongside `--allow-all-tools` — Copilot docs: "Deny rules always take precedence."
> Note: `--deny-tool` governs Copilot's built-ins only. MCP tools are denied via
> `Server(tool)` syntax — supported here automatically as passthrough verbatim names.

> **`--excluded-tools` (declined):** Copilot also exposes `--excluded-tools` (removes tools from
> the model-visible set). It was considered for the web pair, but since Copilot has no
> `web_search`/`web_fetch` tools this is moot; `--deny-tool` for the confirmed `shell`/`write`
> names is the uniform choice.

### 6.5 OpenCode — `opencode_adapter.py`
- **Verify wiring (not a new bug):** `stream()` already accepts `disallowed_tools` in the
  current working tree (added since the first draft of this plan). Just confirm `execute()`
  still forwards it and `stream()` keeps the param — no `TypeError` exists today.
- Map + scoped `OPENCODE_PERMISSION` env var (no global config mutation):

```python
_DISALLOWED_TOOL_MAP = {
    "bash": ["bash"],
    "edit": ["edit"],       # OpenCode collapses write/edit/patch into one `edit` key
    "read": ["read"],
    "web_search": ["websearch"],
    "web_fetch": ["webfetch"],
}
```
- **Merge with any pre-existing `OPENCODE_PERMISSION`** — do not clobber it. A user or CI may
  already export stricter rules; overwriting would silently re-enable previously-denied tools.
  Parse the existing value, layer our denies on top (deny wins), and **fail closed** if the
  existing value is unparseable (still apply our denies rather than dropping them):

```python
native, unsupported = resolve_disallowed_tools(disallowed_tools, _DISALLOWED_TOOL_MAP)
if unsupported:
    warnings.warn(f"OpenCode cannot deny {unsupported}; ignoring", UserWarning, stacklevel=2)

env = None
if native:
    existing = {}
    raw = os.environ.get("OPENCODE_PERMISSION")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing = parsed
            else:
                warnings.warn(
                    "Existing OPENCODE_PERMISSION is not a JSON object; "
                    "applying disallowed_tools denies on top of an empty base",
                    UserWarning, stacklevel=2,
                )
        except json.JSONDecodeError:
            warnings.warn(
                "Existing OPENCODE_PERMISSION is invalid JSON; "
                "applying disallowed_tools denies on top of an empty base",
                UserWarning, stacklevel=2,
            )
    # Our denies win over inherited rules for the same keys. json.dumps stays valid JSON.
    merged = {**existing, **{t: "deny" for t in native}}
    env = {**os.environ, "OPENCODE_PERMISSION": json.dumps(merged)}
```
- Why env var over config-write: `OPENCODE_PERMISSION` is a first-class, regression-tested
  flag (`packages/opencode/src/config/config.ts:544`); `deny` is a hard block that
  short-circuits *before* the approval flow, so it holds even under auto-approve; and it is
  process-scoped, so we never touch the user's `~/.config/opencode/opencode.json`.

> **Shipped behaviour (supersedes the snippet above).** The merge concern landed alongside the
> upstream `$PWD`-pinning fix, so the adapter builds **one** env dict and **always returns it**
> (never `env=None`): `$PWD` is pinned to `cwd` on every run because opencode resolves its project
> dir — and with it the permission boundary — from `$PWD`. Two further hardenings followed:
> **(1)** an inherited bare-string `"deny"` is promoted to the `{"*": "deny"}` wildcard opencode
> actually enforces **even when no deny-list is passed** — re-forwarding the bare string is a
> silent no-op under `--dangerously-skip-permissions`, so it would otherwise fail OPEN on a global
> deny-all; **(2)** with nothing to deny and no bare-`"deny"` to rewrite, the parent env flows
> through untouched (no spurious `OPENCODE_PERMISSION` key). The parse/merge split lives in
> `_build_subprocess_env` + `_inherited_permission` in `opencode_adapter.py`.

### 6.6 Codex — `codex_adapter.py` (special-cased, see §7)

## 7. Codex handling

Codex has no per-tool name-based deny. It can only disable web search via a config
override, and (per our earlier decision) `auto_approve`'s sandbox-bypass rules out the
sandbox route anyway. So:

- Add `disallowed_tools` to `execute()` + `stream()` signatures and forward.
- In `stream()`, before building the command — **warn every call**, not warn-once: a silently
  dropped deny is a security hole, so (unlike `_warned_allowed_tools`) there is no
  `_warned_disallowed_tools` flag:

```python
deny_web_search = bool(disallowed_tools) and "web_search" in disallowed_tools
if disallowed_tools:
    unsupported = sorted(set(disallowed_tools) - {"web_search"})
    if unsupported:
        warnings.warn(
            f"Codex can only deny web_search; ignoring {unsupported}",
            UserWarning, stacklevel=2,
        )
if deny_web_search and effort == "minimal":
    warnings.warn(
        'Codex ignores web_search="disabled" under model_reasoning_effort="minimal" '
        "(openai/codex#5002); the web_search deny will NOT be enforced this call",
        UserWarning, stacklevel=2,
    )
```
- Thread a `deny_web_search: bool` into `_build_command`; when true append
  `["-c", 'web_search="disabled"']`. The value is a TOML **string** mode
  (`disabled`/`cached`/`live`), so it must be quoted — matching the adapter's existing
  `'model_reasoning_effort="{effort}"'` style at `codex_adapter.py:150`. Applies to both
  the fresh-run and `resume` branches.
- Codex uses only the canonical `web_search`; all other names (canonical or verbatim)
  are unsupported → warn. Codex does **not** passthrough verbatim names (no mechanism).

> Known Codex quirk: `web_search="disabled"` is ignored under
> `model_reasoning_effort="minimal"` (openai/codex#5002) — the deny silently fails OPEN at that
> effort. The adapter now emits a **fail-loud `UserWarning` every call** when `web_search` is
> denied at minimal effort (see the snippet above), rather than relying on a code comment.

> **Version fragility (review hardening).** The `web_search` top-level key is verified
> accepted *and* enforced on codex-cli 0.133.0 (a reviewer ran it; the deny holds even under
> `--dangerously-bypass-approvals-and-sandbox`). But it is the *entire* Codex deny capability
> and upstream is moving toward `web_search_mode`, so a future Codex could rename/reject the
> key and silently no-op the deny. Every unit test only asserts agent_shell *emits* the string;
> a single real-process e2e guard in `tests/e2e/test_codex_e2e.py` asserts Codex *accepts* it,
> which is the only test that would catch an upstream key rename.

## 8. TDD test plan (red → green, integration-first)

Conventions to follow (already in the repo): AAA; `asyncio_mode=auto` (no decorator);
mock `asyncio.create_subprocess_exec`; assert command via `mock_exec.call_args[0]`
(positional `*cmd`) and env via `mock_exec.call_args.kwargs["env"]`; warnings via
`pytest.warns(UserWarning, match=...)` and `warnings.catch_warnings(record=True)`.

### New: `tests/unit/test_tool_denial.py` (the shared helper — write first)
- `bash`/`read` map to single native name.
- `edit` fans out to the full modify-family for a given native_map.
- unknown name passes through verbatim.
- canonical name absent from native_map appears in `unsupported`, not `native`.
- duplicate / overlapping inputs are deduped, order preserved.
- `None` / `[]` returns `([], [])`.

### `tests/unit/test_stream.py` (Claude)
- `disallowed_tools=["bash"]` → cmd has `--disallowed-tools` and value `Bash`.
- `["edit"]` → value is `Edit,Write,NotebookEdit` (comma-joined, fan-out).
- `["mcp__foo__bar"]` → passthrough verbatim in the value.
- `None` → no `--disallowed-tools` in cmd.
- combined with `auto_approve=True` → both `--dangerously-skip-permissions` and
  `--disallowed-tools` present.

### `tests/unit/test_copilot_cli_stream.py`
- `["bash"]` → `--deny-tool shell`.
- `["edit"]` → one `--deny-tool write` flag (no `edit` token).
- `["bash","edit"]` → repeated `--deny-tool shell` then `--deny-tool write`.
- `["read","web_search","web_fetch"]` → warns (unsupported), **no** `--deny-tool`.
- `["bash","web_fetch"]` → denies `shell`, warns about `web_fetch`.
- `["view"]` (non-canonical) → passthrough verbatim `--deny-tool view`.
- `None` → no `--deny-tool`.

### `tests/unit/test_opencode_stream.py`
- `await adapter.execute(..., disallowed_tools=["bash"])` runs without error (confirms
  `execute()`→`stream()` stays wired).
- `["bash","web_search"]` → `json.loads(call.kwargs["env"]["OPENCODE_PERMISSION"])
  == {"bash":"deny","websearch":"deny"}`.
- `["edit"]` → env maps only `{"edit":"deny"}` (write/edit collapsed).
- **Env preservation:** with `OPENCODE_PERMISSION='{"bash":"deny","read":"deny"}'` already
  in `os.environ` (patched), `disallowed_tools=["web_search"]` yields a merged
  `{"bash":"deny","read":"deny","websearch":"deny"}` — the inherited denies survive.
- **Deny-wins on conflict:** existing `{"websearch":"allow"}` + `disallowed_tools=["web_search"]`
  → merged `{"websearch":"deny"}`.
- **Fail-closed on bad base:** existing `OPENCODE_PERMISSION="{invalid"` + `["bash"]`
  → warns, and env still contains `{"bash":"deny"}`.
- `None` → `call.kwargs["env"]` is a dict with `$PWD` pinned to `cwd` and **no**
  `OPENCODE_PERMISSION` key (env is never `None`; the parent env flows through). A separate case:
  an inherited bare-`"deny"` promotes to `{"*":"deny"}` even when no deny-list is passed.

### `tests/unit/test_codex_warnings.py`
- `disallowed_tools=["web_search"]` → cmd contains consecutive `-c` and `web_search="disabled"`
  (quoted TOML string), **no** warning.
- `["bash"]` → `pytest.warns(UserWarning, match="web_search")`, no `web_search` config flag.
- `["web_search","bash"]` → both: the `-c web_search="disabled"` flag *and* a warning about `bash`.
- **warn every call** across two `stream()` calls (a dropped security deny must not be suppressed
  by an earlier warning); plus minimal-effort: `disallowed_tools=["web_search"]` with
  `effort="minimal"` warns (fail-open guard), and does **not** warn at other efforts.

### Integration (`tests/integration/test_*_integration.py`)
Through `AgentShell`, assert the deny reaches the subprocess for each adapter (one happy-path
test each), proving the `shell.py` passthrough is wired.

### `tests/unit/test_shell.py`
- `AgentShell.execute`/`stream` forward `disallowed_tools` to the adapter (mock adapter,
  assert kwarg received).

## 9. Edge cases & notes
- **Order of params:** `disallowed_tools` is the **last** parameter on `execute`/`stream`
  (after `session_id`), not adjacent to `allowed_tools`. This keeps existing positional callers
  (`execute(cwd, prompt, allowed_tools, model)`) binding `model`; a regression test
  (`test_positional_args_bind_model_not_disallowed_tools`) pins it.
- **`allowed_tools` + `disallowed_tools` together** are left to each CLI (all document
  deny-wins). No cross-validation in `agent_shell` — YAGNI.
- **OpenCode bare-string `"deny"` is a no-op via the env var.** `OPENCODE_PERMISSION='"deny"'`
  is silently dropped (opencode runs the raw `JSON.parse` through remeda `mergeDeep`, which
  discards a primitive — verified on 1.14.41 and proven by a real run that executed a "denied"
  command). So an inherited bare `"deny"` is **promoted to the object wildcard `{"*":"deny"}`**,
  which *is* honored, with our explicit per-tool denies merged on top. We only ever emit the
  object form.
- **OpenCode fails closed** on a malformed/unmergeable inherited `OPENCODE_PERMISSION` (warns,
  applies our denies on an empty base) and we only ever build the emitted value with
  `json.dumps`, so what we pass is always valid JSON.
- **OpenCode env also pins `PWD`** to the resolved `cwd` (opencode reads its project dir — and
  permission boundary — from `$PWD`); the deny env and the PWD pin share one `env=` dict.
- **Typo guard (suggested).** Passthrough is an escape hatch, but it also means a typo like
  `"websearch"` (vs canonical `web_search`) silently denies nothing on adapters that don't use
  that literal. Optionally have `resolve_disallowed_tools` warn when a passthrough name is a
  close match to a canonical one (e.g. equal after stripping `_`/`-`). Cheap safety net; add a
  test for `["websearch"]` → warning. Keep the name flowing through (don't auto-correct).
- **`gemini_cli`** is in `AgentType` but has no adapter — out of scope.
- Update `docs/development/agent_parameter_comparison.md` with the `disallowed_tools`
  support row once implemented.

## 10. Out of scope (YAGNI)
- Codex sandbox-based denial of shell/file-writes (void under `auto_approve=True`).
- Codex MCP per-server `disabled_tools` (needs server-qualified names; add when a caller
  needs it).
- Making the silently-ignored `allowed_tools` on OpenCode consistent (tracked separately).
- Extending the canonical set beyond the core five (`glob`, `grep`, `task`, …) until needed.

> Note: wiring OpenCode's `auto_approve` to `--dangerously-skip-permissions` (opencode ≥1.14.41)
> was previously listed here as out-of-scope drift. It has since been fixed upstream and
> integrated; `auto_approve=True` now maps to the flag, and the deny-list holds alongside it
> (deny short-circuits before approvals — verified). No longer outstanding.

## 11. Implementation sequence
1. `tests/unit/test_tool_denial.py` (red) → `adapters/tool_denial.py` (green).
2. `shell.py` passthrough + `test_shell.py`.
3. Claude → Copilot → OpenCode (incl. the `stream()` `TypeError` regression) → Codex,
   each test-first.
4. Integration happy-path per adapter.
5. Update `agent_parameter_comparison.md`.
6. Full `pytest` (unit + integration); e2e remains manual/out-of-CI.
