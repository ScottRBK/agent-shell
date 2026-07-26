"""The single success rule every normalized stream is judged by.

The CLI probes showed there is no reliable raw signal (exit code lies — opencode returns 0
on failure; stderr placement is inconsistent), but every adapter already normalizes
outcomes into the StreamEvent contract, so the rule is expressed purely over events:

    success  <=>  the LAST `result` event says "ok" and no `error` event arrived.

"The last one" rather than "any one", because a single run can emit several. pi's
auto-retry is on by default (maxRetries 3): a retryable provider fault emits an agent_end
with willRetry=true, then the session continues the agent, which emits its own agent_end —
so a run the agent itself recovered from carries a failing result followed by an ok one.
Auto-compaction continues the agent the same way. The collector already reads its metrics
from the last result for exactly this reason; the verdict has to agree with it.

Both surfaces that report an outcome apply it from here — `health_check` via health.py and
`execute` via response.py — so neither can drift and a caller sees the same wording for the
same failure whichever one they used.
"""

from typing import Iterable

from agent_shell.models.agent import StreamEvent


def failure_reason(events: Iterable[StreamEvent]) -> str | None:
    """None if the run succeeded; otherwise the best available reason it failed.

    A failing run can leave more than one trace behind, so the most informative one wins
    rather than the earliest:

      1. a failing `result`'s `error` — a reason the adapter recovered from the harness's
         own structured output (pi reads it out of agent_end). This outranks the `error`
         event because that event is usually just the stderr tail of a non-zero exit, and
         a node CLI's stderr tail is routinely unrelated noise; ranking it first threw the
         recovered reason away on every non-zero exit (issue #10's fix, defeated).
      2. an `error` event's `content` — the harness said outright what went wrong. Codex's
         `turn.failed` and opencode's stream-level error arrive this way and carry real
         detail, and neither of those adapters sets `result.error`, so they are unaffected
         by rule 1 sitting above them.
      3. "agent reported an error result" — it failed and nothing said why;
      4. "no result event received" — nothing terminal ever arrived, so the run was cut
         short (a turn can truncate with no result AND no error, and still exit 0).

    Within each kind the FIRST occurrence wins: a later `error` event is usually an echo,
    and when several attempts failed the earliest reason is the root cause rather than
    whatever the run degenerated into.
    """
    last_result_failed: bool | None = None   # None = no `result` event ever arrived
    event_reason: str | None = None
    result_detail: str | None = None

    for event in events:
        if event.type == "error":
            if event_reason is None:
                event_reason = event.content or "unknown error"
        elif event.type == "result":
            last_result_failed = event.content != "ok"
            if last_result_failed and result_detail is None:
                result_detail = event.error

    if last_result_failed is None:
        return event_reason or "no result event received"
    if not last_result_failed:
        # The run ended well. Only an `error` event can still condemn it — a non-zero exit
        # means the CLI itself failed, whatever its last result claimed.
        return event_reason
    return result_detail or event_reason or "agent reported an error result"
