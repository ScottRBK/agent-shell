"""Shared stream-to-response collection for all adapters.

Every adapter's `execute` delegates here, the way every `health_check` delegates to
health.py. The collection was byte-for-byte identical in all six adapters, and so was the
bug in it: it kept the text and the metrics and threw the run's outcome away, so a failed
run came back as a success-shaped AgentResponse with an empty answer (issue #11).

Aggregation rules that must not drift:

  - text:       every `text` event, newline-joined, in arrival order;
  - metrics:    the LAST `result` event;
  - session id: the FIRST event that carries one.

"Last result" is not a formality: pi emits one agent_end per agent loop, and both its
auto-retry (on by default) and its auto-compaction continue the agent into another loop, so
a single run really can produce several. outcome.py judges the same last-one-wins way, so
the metrics a caller gets always belong to the attempt that decided the verdict. (opencode
emits its terminal `step_finish` once per run; the other four CLIs are single-result too.)

The outcome itself is judged by the shared rule in outcome.py, so a failed run raises
AgentExecutionError — carrying that same partial data, so raising destroys nothing.
"""

from agent_shell.adapters.outcome import failure_reason
from agent_shell.models.agent import AgentExecutionError, AgentResponse, StreamEvent


async def collect_response(
        adapter,
        cwd: str,
        prompt: str,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        include_thinking: bool = False,
        auto_approve: bool = True,
        session_id: str | None = None,
        disallowed_tools: list[str] | None = None,
) -> AgentResponse:
    chunks: list[StreamEvent] = []
    async for event in adapter.stream(
        cwd=cwd,
        prompt=prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=model,
        effort=effort,
        include_thinking=include_thinking,
        auto_approve=auto_approve,
        session_id=session_id,
    ):
        chunks.append(event)

    text = "\n".join(e.content for e in chunks if e.type == "text")
    cost = next((e.cost for e in reversed(chunks) if e.type == "result"), 0.0)
    duration = next((e.duration for e in reversed(chunks) if e.type == "result"), 0.0)
    output_tokens = next((e.output_tokens for e in reversed(chunks) if e.type == "result"), 0)
    returned_session_id = next((e.session_id for e in chunks if e.session_id), None)

    reason = failure_reason(chunks)
    if reason is not None:
        raise AgentExecutionError(
            reason, response=text, cost=cost, session_id=returned_session_id,
            duration=duration, output_tokens=output_tokens,
        )

    return AgentResponse(
        response=text, cost=cost, session_id=returned_session_id,
        duration=duration, output_tokens=output_tokens,
    )
