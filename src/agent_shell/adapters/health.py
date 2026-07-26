"""Shared health-probe logic for all adapters.

Every adapter's `health_check` delegates here, so the probe lives in one place. Whether
the run succeeded is not decided here though: that is the same question `execute` asks, so
both read the verdict from outcome.py and a health failure and an execution failure are
worded identically.

    healthy  <=>  the LAST `result` event has content == "ok" and no `error` event arrived.

Last, not any: Pi auto-retries, so a run can emit `result(error)` then `result(ok)` — or the
reverse when a continuation fails. Only the final verdict counts.

A trivial prompt is sent with no tools; only the terminal event matters, never the text.
"""

import asyncio
import logging

from agent_shell.adapters.outcome import failure_reason
from agent_shell.models.agent import HealthCheckResult, StreamEvent

logger = logging.getLogger(__name__)

# Minimal prompt — just enough to elicit one completed turn. The response text is
# never inspected, so this stays as cheap as possible.
HEALTH_PROMPT = "Reply with: ok"


async def run_health_probe(
        adapter,
        cwd: str,
        model: str | None = None,
        timeout: float = 60.0,
) -> HealthCheckResult:
    events: list[StreamEvent] = []

    async def _consume() -> None:
        async for event in adapter.stream(
                cwd=cwd,
                prompt=HEALTH_PROMPT,
                model=model,
                allowed_tools=[],
                auto_approve=True,
        ):
            events.append(event)

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        await adapter.cancel()
        logger.warning("Health check timed out after %.1fs (model=%s)", timeout, model)
        return HealthCheckResult(
            healthy=False, exception=f"health check timed out after {timeout}s",
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        await adapter.cancel()
        raise
    except Exception as e:  # noqa: BLE001 - any spawn/transport failure means unhealthy
        logger.warning("Health check failed (model=%s): %s", model, e)
        return HealthCheckResult(healthy=False, exception=str(e) or repr(e))

    reason = failure_reason(events)
    if reason is None:
        return HealthCheckResult(healthy=True, exception=None)
    return HealthCheckResult(healthy=False, exception=reason)
