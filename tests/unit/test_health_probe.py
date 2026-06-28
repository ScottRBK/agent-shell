"""Unit tests for the shared health-probe classifier.

`run_health_probe` is the single implementation every adapter delegates to. It
consumes an adapter's `stream()` and applies the health rule. A fake adapter lets
us exercise the rule (and the timeout/exception paths) without any subprocess.
"""

import asyncio

import pytest

from agent_shell.models.agent import StreamEvent, HealthCheckResult
from agent_shell.adapters.health import run_health_probe


class FakeAdapter:
    """Minimal AgentAdapter stand-in: yields scripted StreamEvents from stream()."""

    def __init__(self, events=None, raise_exc=None, hang=False):
        self._events = events or []
        self._raise_exc = raise_exc
        self._hang = hang
        self.cancelled = False

    async def stream(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._hang:
            await asyncio.sleep(5)
        for event in self._events:
            yield event

    async def cancel(self):
        self.cancelled = True


class TestHealthRule:
    async def test_ok_result_with_no_error_is_healthy(self):
        # Arrange
        adapter = FakeAdapter([
            StreamEvent(type="text", content="ok"),
            StreamEvent(type="result", content="ok"),
        ])

        # Act
        result = await run_health_probe(adapter, cwd="/tmp", model="m")

        # Assert
        assert result == HealthCheckResult(healthy=True, exception=None)

    async def test_error_result_is_unhealthy(self):
        # Arrange
        adapter = FakeAdapter([StreamEvent(type="result", content="error")])

        # Act
        result = await run_health_probe(adapter, cwd="/tmp", model="m")

        # Assert
        assert result.healthy is False
        assert result.exception is not None

    async def test_error_event_captures_its_content_as_exception(self):
        # Arrange
        adapter = FakeAdapter([StreamEvent(type="error", content="provider unreachable")])

        # Act
        result = await run_health_probe(adapter, cwd="/tmp", model="m")

        # Assert
        assert result.healthy is False
        assert result.exception == "provider unreachable"

    async def test_no_result_event_is_unhealthy(self):
        # Arrange — stream ends with only chatter, never a terminal result.
        adapter = FakeAdapter([StreamEvent(type="text", content="hi")])

        # Act
        result = await run_health_probe(adapter, cwd="/tmp", model="m")

        # Assert
        assert result.healthy is False
        assert result.exception is not None


class TestHealthProbeLifecycle:
    async def test_timeout_returns_unhealthy_and_cancels(self):
        # Arrange
        adapter = FakeAdapter(hang=True)

        # Act
        result = await run_health_probe(adapter, cwd="/tmp", model="m", timeout=0.05)

        # Assert
        assert result.healthy is False
        assert "tim" in result.exception.lower()  # "timed out" / "timeout"
        assert adapter.cancelled is True

    async def test_adapter_exception_is_captured(self):
        # Arrange
        adapter = FakeAdapter(raise_exc=RuntimeError("spawn failed"))

        # Act
        result = await run_health_probe(adapter, cwd="/tmp", model="m")

        # Assert
        assert result.healthy is False
        assert "spawn failed" in result.exception

    async def test_cancellation_propagates_after_cleanup(self):
        # Arrange
        adapter = FakeAdapter(raise_exc=asyncio.CancelledError())

        # Act / Assert — cancellation is not swallowed into a result.
        with pytest.raises(asyncio.CancelledError):
            await run_health_probe(adapter, cwd="/tmp", model="m")
        assert adapter.cancelled is True
