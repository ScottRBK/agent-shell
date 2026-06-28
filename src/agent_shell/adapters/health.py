"""Shared health-probe logic for all adapters.

Every adapter's `health_check` delegates here, so the rule lives in one place. The
CLI probes showed there is no reliable raw signal (exit code lies — opencode returns
0 on failure; stderr placement is inconsistent), but every adapter already normalizes
outcomes into the StreamEvent contract. So the rule is expressed purely over events:

    healthy  <=>  a `result` event with content == "ok" arrives and no `error` event.

A trivial prompt is sent with no tools; only the terminal event matters, never the text.
"""

import asyncio
import logging

from agent_shell.models.agent import HealthCheckResult

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
    saw_ok_result = False
    error_detail: str | None = None

    async def _consume() -> None:
        nonlocal saw_ok_result, error_detail
        async for event in adapter.stream(
                cwd=cwd,
                prompt=HEALTH_PROMPT,
                model=model,
                allowed_tools=[],
                auto_approve=True,
        ):
            if event.type == "error":
                if error_detail is None:
                    error_detail = event.content or "unknown error"
            elif event.type == "result":
                if event.content == "ok":
                    saw_ok_result = True
                elif error_detail is None:
                    error_detail = "agent reported an error result"

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

    healthy = saw_ok_result and error_detail is None
    if healthy:
        return HealthCheckResult(healthy=True, exception=None)
    return HealthCheckResult(
        healthy=False, exception=error_detail or "no result event received",
    )
