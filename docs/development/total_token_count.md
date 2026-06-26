# Implementation Plan: `output_tokens` (response generation token count)

> Status: implemented (2026-06-26). All four adapters + models, unit/integration/e2e tests landed
> following this plan; CI suite (unit + integration) green at 331 tests. E2e canaries added but not
> run here (gated, require live CLIs/credentials).
> Canonical identifier in code: **`output_tokens: int = 0`** on both `StreamEvent` and
> `AgentResponse`.
> The original request said "total_tokens"; investigation (see §3) narrowed the intent to
> **output tokens only** — the tokens the model generated for the response, not prompt /
> cache / reasoning accounting. The file is named `total_token_count.md` per request; the
> canonical field name is `output_tokens`.

## 1. Goal

Expose, on every adapter, how many tokens the model **generated** for a run — surfaced as a
single integer on the streamed `result` event and on the aggregated `AgentResponse`:

```python
resp = await shell.execute(cwd=".", prompt="...")
resp.output_tokens        # int — generated tokens for this run, 0 if the harness can't report it
```

Output tokens (not input/total) because that is what the user cares about: "what the
response generated". Input, cache, and reasoning accounting differ wildly between harnesses
and are explicitly **out of scope**.

## 2. Current state

`models/agent.py`:

```python
@dataclass
class AgentResponse:
    response: str
    cost: float
    session_id: str | None = None
    duration: float = 0.0

@dataclass
class StreamEvent:
    type: str
    content: str
    cost: float = 0.0
    duration: float = 0.0
    session_id: str | None = None
```

`cost` and `duration` are the precedent: optional, default-valued, carried on the `result`
StreamEvent, and aggregated in `execute()` via "take the last `result` event". Each
adapter's `execute()` is byte-for-byte identical in that aggregation.

## 3. Evidence (live sessions, not fixtures)

All four CLIs were run for real — both a trivial single-turn prompt and a multi-step
tool-using prompt (write two files, read them back). Raw captures live under
`scratchpad/tokcheck/` (single) and `scratchpad/tokcheck/multi/` (multi-step). Key finding:
**the repo's Claude fixture was stale** — Claude Code *does* report token usage — and the
multi-step run revealed that output tokens fall into **two aggregation regimes**.

### Where output tokens live (verified)

| Harness | Field path | Event carrying it |
|---|---|---|
| Claude Code | `result.usage.output_tokens` | single `result` event (cumulative) |
| Codex | `turn.completed.usage.output_tokens` | single `turn.completed` (whole `exec` = 1 turn) |
| OpenCode | `step_finish.part.tokens.output` **+** `.tokens.reasoning` | **every** `step_finish` (per step) |
| Copilot | `assistant.message.data.outputTokens` | **every** `assistant.message` (per message) |

> **Reasoning inclusivity (verified by source inspection — see §4 D4).** Claude, Codex, and
> Copilot all report a *reasoning-inclusive* output figure directly (reasoning is a subset of the
> reported output). OpenCode is the exception: `tokens.output` has reasoning **subtracted out**
> (`session.ts`: `output = outputTokens − reasoningTokens`) and reports it in the sibling
> `tokens.reasoning`, so the adapter sums `output + reasoning` to recover the billed figure. The two
> OpenCode fields are disjoint (no double-count); for Anthropic models `reasoning` is 0.

### The two regimes (multi-step run)

| Harness | output seen per event | take-LAST | correct total | take-last error |
|---|---|---|---|---|
| Claude Code | one cumulative `565` on `result` | 565 | **565** | none |
| Codex | one turn → `182` | 182 | **182** | none |
| OpenCode | `286, 193, 42` over 3 `step_finish` | 42 | **521** | ~12× under |
| Copilot | `618, 71, 201, 36` over 4 `assistant.message` | 36 | **926** | ~26× under |

Conclusions that drive the design:

1. **Claude** — use `result.usage.output_tokens`. It is cumulative and authoritative. Do
   **not** sum per-`assistant`-message usages: they summed to 301 (≠ 565) and contained
   duplicate-looking events.
2. **Codex** — `codex exec` emits exactly one `turn.completed`; its `output_tokens` is the
   whole-run figure. (Also: the adapter currently discards the entire `usage` dict —
   `cost=0.0, duration=0.0` hardcoded.)
3. **OpenCode** — output is **per step and not cumulative**. The adapter today only emits a
   `result` StreamEvent when `reason == "stop"`, and only the *final* step has that reason
   (output=42); the two real work steps (`reason="tool-calls"`, output 286 & 193) are
   dropped. Correct total requires reading output from **all** `step_finish` events. Also,
   OpenCode subtracts reasoning out of `tokens.output`, so for the cost figure each step adds
   back the sibling `tokens.reasoning` (per D4).
4. **Copilot** — `result.usage` has **no token fields at all**; output exists only on each
   `assistant.message`. Must sum across messages.

## 4. Design decisions (locked)

**D1 — Field name & type.** `output_tokens: int = 0` on both dataclasses. `int` (not
`int | None`) to match the `cost`/`duration` "default-to-zero when unsupported" convention.
`0` means "not reported / none generated".

**D2 — Carrier.** The count rides on the `result` StreamEvent, exactly like `cost`/
`duration`.

**D3 — Each adapter owns its own summing.** The per-harness divergence (cumulative vs
per-step vs per-message) is hidden **inside each adapter's `stream()`**, which emits the
final authoritative total on its `result` event. This keeps `execute()` uniform — it
continues to "take the last `result` event" with no per-adapter branching. This is the
central decision: do **not** push summing into `execute()` (it cannot be uniform — Claude is
cumulative while OpenCode/Copilot are additive).

**D4 — Include reasoning tokens (this is a COST measure).** *(Revised — supersedes the draft's
"exclude reasoning" stance.)* The purpose of `output_tokens` is cost measurement, and reasoning
tokens are **billed at the output-token rate**. So the reported figure must INCLUDE them. Codex
mirrors the OpenAI Responses API where `usage.output_tokens` already includes reasoning
(`reasoning_output_tokens` is a subset of it, and `total = input + output`), so the adapter reports
`output_tokens` **raw** — no subtraction. The fixture (`output_tokens=22, reasoning_output_tokens=14`)
therefore expects **22**.

All four adapters end up reasoning-**inclusive**, but the source they read differs (verified by
source inspection):

- **Claude, Codex, Copilot** report a reasoning-inclusive output figure *directly* — reasoning is a
  subset of the reported output (Codex/OpenAI: `total = input + output`; Copilot's `outputTokens` is
  the API `completion_tokens`, with `completion_tokens_details.reasoning_tokens` nested inside it).
  These adapters read the field as-is; adding any separate reasoning field would **double-count**.
- **OpenCode** is the exception: it subtracts reasoning *out* of `tokens.output` and reports it in a
  disjoint `tokens.reasoning` sibling, so its adapter sums `output + reasoning` to reconstruct the
  billed figure. (For Anthropic models `tokens.reasoning` is 0.)

Input and cache counts remain a separate cost component, out of scope for this field.

> **History:** an earlier draft of this doc said "exclude reasoning (visible answer)". That was the
> wrong lens — for cost you want the billed figure. The Codex adapter briefly subtracted reasoning
> during implementation; that was reverted once the cost intent was confirmed. A later verification
> pass (multi-agent source inspection of sst/opencode and @github/copilot, adversarially checked)
> found OpenCode alone *excludes* reasoning from its output field and fixed it to add it back.

**D5 — Intermediate StreamEvents carry `output_tokens=0`.** Only the `result` event carries
the total. We do not retrofit per-step counts onto `text`/`tool_use` events — YAGNI; no
consumer needs mid-stream token counts today.

**D6 — Protocol unchanged.** `AgentAdapter` defines method signatures, not return-field
shapes. No change to `agent_adapter_protocol.py`.

## 5. Changes by file

### 5.1 `models/agent.py`

Add the field to both dataclasses (last field, after `session_id`):

```python
@dataclass
class AgentResponse:
    response: str
    cost: float
    session_id: str | None = None
    duration: float = 0.0
    output_tokens: int = 0

@dataclass
class StreamEvent:
    type: str
    content: str
    cost: float = 0.0
    duration: float = 0.0
    session_id: str | None = None
    output_tokens: int = 0
```

### 5.2 `execute()` aggregation — identical edit in all four adapters

Add one line alongside the existing `cost`/`duration` take-last, and pass it through:

```python
output_tokens = next(
    (e.output_tokens for e in reversed(chunks) if e.type == "result"), 0
)
return AgentResponse(
    response=text, cost=cost, session_id=returned_session_id,
    duration=duration, output_tokens=output_tokens,
)
```

### 5.3 `claude_code_adapter.py` — `_parse_event`, `result` branch

```python
elif t == "result":
    cost = event.get("total_cost_usd", 0) or 0
    duration = (event.get("duration_ms", 0) or 0) / 1000
    output_tokens = event.get("usage", {}).get("output_tokens", 0) or 0
    is_error = event.get("is_error", False)
    status = "error" if is_error else "ok"
    events.append(StreamEvent(
        type="result", content=status, cost=cost, duration=duration,
        session_id=session_id, output_tokens=output_tokens,
    ))
```

Stateless — Claude's `result.usage.output_tokens` is already cumulative.

### 5.4 `codex_adapter.py` — `_parse_event`, `turn.completed` branch

```python
elif t == "turn.completed":
    output_tokens = event.get("usage", {}).get("output_tokens", 0) or 0
    events.append(StreamEvent(
        type="result", content="ok", cost=0.0, duration=0.0,
        output_tokens=output_tokens,
    ))
```

Stateless — one `turn.completed` per `exec`. Per D4 (cost measure), `output_tokens` is reported
**raw** — it already includes reasoning, which is billed at the output rate. (`usage` coalesced via
`or {}` to tolerate a null usage object.)

### 5.5 `opencode_adapter.py` — requires accumulation (behaviour change)

OpenCode output is per-step and the adapter only emits a `result` on `reason == "stop"`.
Accumulate across **all** `step_finish` events within a single `stream()` call, then attach
the running total to the final (stop) `result` event.

- Add a local accumulator in `stream()` (a closure-local `int`, reset per `stream()` call —
  **not** instance state, since adapters are reused across calls).
- `_parse_event` currently takes only `(event, include_thinking)` and is stateless. Two
  options:
  - **(a)** Have `stream()` pass a mutable accumulator into `_parse_event` and add the
    step's output on every `step_finish` (any `reason`), emitting the total only on
    `reason == "stop"`.
  - **(b)** Sum in `stream()` directly: on every `step_finish` read
    `part.tokens.output`; when `reason == "stop"` emit the `result` event with the
    accumulated total.

  Prefer **(b)** — keeps `_parse_event` pure and confines the new statefulness to the
  `stream()` loop. Sketch:

  ```python
  # inside stream(), before the read loop:
  run_output_tokens = 0
  ...
  # where lines are parsed, accumulate billed output (output + reasoning) per step_finish:
  run_output_tokens += self._step_output_tokens(raw)   # 0 for non-step_finish events
  for event in self._parse_event(event=raw, include_thinking=include_thinking,
                                 run_output_tokens=run_output_tokens):
      yield event
  ```

  with `_step_output_tokens` returning `tokens.output + tokens.reasoning` (reasoning is excluded
  from OpenCode's `output`, see §4 D4) and `_parse_event` stamping `output_tokens=run_output_tokens`
  only on the `reason == "stop"` result branch. The invariant is: **sum every
  `step_finish.part.tokens.output + .tokens.reasoning`, emit on stop**.

> Note the existing `cost` extraction (`part.cost` on the stop step) is left as-is — this
> plan is scoped to output tokens. Whether OpenCode's stop-step `cost` is itself a per-step
> undercount is a **separate** question flagged for follow-up, not addressed here.

### 5.6 `copilot_cli_adapter.py` — requires accumulation (new parsing)

`result.usage` has no tokens; sum `assistant.message.data.outputTokens` across the run. The
`assistant.message` branch is currently parsed only for `toolRequests`; extend it to feed an
accumulator, and stamp the total on the `result` event.

- Local accumulator in `stream()` (reset per call).
- On each `assistant.message`, add `data.outputTokens`.
- On `result`, emit with `output_tokens=<accumulated>`.

Same threading choice as OpenCode; keep `_parse_event` pure by accumulating in the
`stream()` loop and passing the running total into the `result` branch.

> Unlike OpenCode, Copilot's `outputTokens` is the API `completion_tokens` and **already
> includes** reasoning (reasoning is nested inside it as `completion_tokens_details.reasoning_tokens`).
> A separate `reasoningTokens` appears only on `assistant.usage` events as an informational
> breakdown — do **not** add it, or the cost would double-count (verified by source inspection).

## 6. Test plan (TDD — red first)

Integration-first per project discipline. Each adapter has: `*_fixtures.py`,
`test_*_parse_event.py`, `test_*_execute.py` (Claude's are `fixtures.py`,
`test_parse_event.py`, `test_execute.py`). Real captures from §3 become the fixture data.

### 6.1 Model

- `test` (new, e.g. in an existing models test or `test_execute.py`): `StreamEvent` and
  `AgentResponse` default `output_tokens` to `0`; constructible with an explicit value.

### 6.2 Per-adapter `_parse_event` (Claude, Codex — stateless)

- Update `RESULT_EVENT_SUCCESS` (Claude `fixtures.py`) to include the real
  `usage: {"output_tokens": 4, ...}` captured from the live run.
- Update Codex `turn.completed` fixture to include `usage: {"output_tokens": 20, ...}`.
- Assert the emitted `result` StreamEvent has `output_tokens` == expected.
- Add a "missing usage" case → `output_tokens == 0` (back-compat / defensive).

### 6.3 OpenCode & Copilot — multi-step accumulation (the important ones)

These are where take-last fails, so the regression guard matters most.

- **OpenCode** (`test_opencode_execute.py` / `test_opencode_stream.py`): feed a fixture
  sequence of **three** `step_finish` events with outputs `286, 193, 42` and reasons
  `tool-calls, tool-calls, stop` (from `scratchpad/tokcheck/multi/opencode`). Assert the
  aggregated `AgentResponse.output_tokens == 521` (**not** 42). This test fails today and
  fails against any "stop-only" implementation — it is the guard for §5.5.
- **Copilot** (`test_copilot_cli_execute.py`): feed four `assistant.message` events with
  `outputTokens` `618, 71, 201, 36` plus a tokenless `result`. Assert
  `AgentResponse.output_tokens == 926` (**not** 36, **not** 0).
- Add single-step cases for both (sum of one == that one).

### 6.4 `execute()` aggregation (all four)

- Assert `AgentResponse.output_tokens` equals the `result` event's value (take-last) — the
  uniform path from §5.2.

### 6.5 E2E (required — not optional)

E2E is **mandatory** here, not a nice-to-have. The stale-fixture bug (§3, §7) existed
*because* the only proof was a hand-captured fixture; unit tests against that fixture would
have happily confirmed the wrong answer forever. A unit test proves "we parse the field we
think exists"; only a real run proves "the CLI still emits that field." These keys
(`output_tokens` / `outputTokens` / `tokens.output`) are vendor-specific and version-fragile,
and every one degrades silently to `0` on a rename — exactly the failure an e2e canary
exists to catch. Same role the Codex `web_search` e2e plays for the deny path.

Each harness already has `tests/e2e/test_<harness>_e2e.py` gated with
`pytestmark = pytest.mark.e2e`. Add to **each**:

- **Single-turn canary (all four):** a real `execute()` whose response is non-trivial,
  asserting `response.output_tokens > 0`. This fails the moment a CLI renames or drops its
  usage field — the regression we must never ship blind.

  ```python
  class TestOutputTokensE2E:
      async def test_execute_reports_output_tokens(self):
          # Arrange
          shell = AgentShell(agent_type=AgentType.OPENCODE)  # per-file harness
          # Act
          response = await shell.execute(
              cwd="/tmp",
              prompt="Write a short paragraph about the sea.",
              allowed_tools=[],
          )
          # Assert
          assert response.output_tokens > 0, (
              "No output tokens from a real run — the CLI's usage field may have "
              "been renamed/dropped; re-verify the field path in the adapter"
          )
  ```

- **Multi-step accumulation guard (OpenCode + Copilot — required):** the two summing
  adapters get a real *tool-using* run (e.g. write two files, read them back) asserting the
  total exceeds what any single step/message could contribute. This is the live counterpart
  to the unit guards in §6.3 and the only thing that proves accumulation works against the
  CLI's actual multi-step event stream — not just our captured fixture.

  ```python
      async def test_multistep_accumulates_output_tokens(self, tmp_path):
          shell = AgentShell(agent_type=AgentType.OPENCODE)
          response = await shell.execute(
              cwd=str(tmp_path),
              prompt=(
                  "Create one.txt containing 'alpha', create two.txt containing 'beta', "
                  "read both back, then tell me the two words."
              ),
          )
          # A multi-step run must sum across steps/messages — a take-last bug caps this low.
          assert response.output_tokens > 100, (
              "Multi-step output tokens implausibly low — accumulation across "
              "step_finish/assistant.message events likely regressed to take-last"
          )
  ```

  (Threshold is a loose plausibility floor, not an exact count — real token counts vary run
  to run. The captured multi-step runs were 521 / 926; `> 100` catches a take-last
  regression, which would cap at the final step/message — 42 / 36 in our captures — without
  being flaky on legitimate variation. Claude and Codex report cumulatively, so the
  single-turn canary already covers them; a multi-step assertion adds nothing there.)

## 7. Regression / risk notes

- **Stale-fixture lesson.** The first analysis trusted `fixtures.py` and wrongly concluded
  Claude reports no tokens. Fixtures must be refreshed from live captures (done in §3) and
  the e2e canary (§6.5) guards against future drift.
- **Adapter reuse.** Adapters are long-lived and reused across calls. The OpenCode/Copilot
  accumulators **must** be local to each `stream()` invocation, never instance attributes,
  or counts leak between runs. A test that calls `execute()` twice on one adapter instance
  and asserts the second run's count is independent will catch this.
- **Version fragility.** `output_tokens` / `outputTokens` / `tokens.output` are
  vendor-specific keys; all use `.get(..., 0)` so a rename degrades to `0`, never crashes.
- **Scope discipline.** Reasoning tokens are **in** scope and **included** in the billed
  output figure (D4) — that is the whole point of a cost measure. Input tokens, cache tokens,
  and OpenCode's possibly-undercounted `cost` remain out of scope (§5.5 note).
- **Reasoning inclusivity is vendor-specific.** Three harnesses fold reasoning into their
  output field; OpenCode alone splits it out. This was confirmed by source inspection of
  `sst/opencode` and `@github/copilot` (multi-agent, adversarially verified), not assumed. If a
  vendor changes how it accounts reasoning, the e2e multi-step canaries are the drift guard, but
  re-verification from source is warranted on a major CLI upgrade.

## 8. Summary of effort

| Harness | Change | Effort |
|---|---|---|
| Models | add field ×2 | trivial |
| Claude | read `result.usage.output_tokens` + fix stale fixture | small |
| Codex | wire up discarded `usage` dict | small |
| OpenCode | accumulate across all `step_finish`, emit on stop | **medium** (behaviour change) |
| Copilot | accumulate across `assistant.message`, emit on result | **medium** (new parsing) |
| `execute()` | one take-last line ×4 | trivial |
| E2E (**required**) | `output_tokens > 0` canary ×4 + multi-step accumulation guard ×2 | small, but mandatory (§6.5) |

The field is feasible and clean everywhere; the real work is the OpenCode and Copilot
accumulation and their multi-step regression tests. E2E coverage is **not optional** — it is
the only layer that catches a CLI silently renaming or dropping its usage field, the exact
class of bug that made the original fixture-based analysis wrong.
