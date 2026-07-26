"""The collector's aggregation rules, which response.py's own docstring says must not drift.

Nothing pinned them: every other suite feeds a stream with ONE result event and ONE
session-bearing event, so "first" and "last" are the same answer and any of these rules
could be inverted without a single test noticing. They only diverge on a stream that
carries several of each, which is not exotic — pi emits an agent_end per agent loop and
its auto-retry and auto-compaction both run more than one.

These drive `collect_response` directly against a hand-built event sequence rather than
through a subprocess: the sequences below are the point of the test, and no CLI can be
made to emit a chosen pair of session ids on demand. `collect_response` takes the adapter
as a parameter precisely because the stream is its input, so the stub is the real seam,
not a mock of collaborator behaviour.
"""

import pytest

from agent_shell.adapters.response import collect_response
from agent_shell.models.agent import AgentExecutionError, StreamEvent

FIRST_SESSION = "019f0ae6-995e-780b-b2e7-f00d2d72873f"
LATER_SESSION = "019f0b11-4a20-7c3d-9e88-11c2b4a7d001"


class _StubAdapter:
    """Yields a fixed event sequence. Accepts (and ignores) collect_response's kwargs."""

    def __init__(self, events: list[StreamEvent]):
        self._events = events

    async def stream(self, **_kwargs):
        for event in self._events:
            yield event


def _two_result_stream(last_content: str = "ok") -> list[StreamEvent]:
    """A run with two result events and two session-bearing events, all values distinct."""
    return [
        StreamEvent(type="system", content="", session_id=FIRST_SESSION),
        StreamEvent(type="text", content="first answer"),
        StreamEvent(type="result", content="ok", cost=0.5, duration=1.5, output_tokens=11,
                    session_id=FIRST_SESSION),
        StreamEvent(type="text", content="second answer"),
        StreamEvent(type="result", content=last_content, cost=0.25, duration=9.5,
                    output_tokens=42, session_id=LATER_SESSION),
    ]


async def _collect(events: list[StreamEvent]):
    return await collect_response(_StubAdapter(events), cwd="/tmp", prompt="ping")


class TestMetricsComeFromTheLastResult:
    async def test_cost_is_the_last_results(self):
        # Arrange
        events = _two_result_stream()

        # Act
        response = await _collect(events)

        # Assert — 0.25, not the earlier 0.5.
        assert response.cost == 0.25

    async def test_duration_is_the_last_results(self):
        # Arrange
        events = _two_result_stream()

        # Act
        response = await _collect(events)

        # Assert
        assert response.duration == 9.5

    async def test_output_tokens_are_the_last_results(self):
        # Arrange
        events = _two_result_stream()

        # Act
        response = await _collect(events)

        # Assert
        assert response.output_tokens == 42

    async def test_the_same_metrics_ride_on_the_exception_when_the_run_failed(self):
        # Arrange — the failure path aggregates separately, so it needs its own guard.
        events = _two_result_stream(last_content="error")

        # Act
        with pytest.raises(AgentExecutionError) as excinfo:
            await _collect(events)

        # Assert
        assert excinfo.value.cost == 0.25
        assert excinfo.value.duration == 9.5
        assert excinfo.value.output_tokens == 42


class TestSessionIdComesFromTheFirstEventThatHasOne:
    async def test_the_earliest_session_id_wins(self):
        # Arrange — the id a caller passes back as session_id must identify the session the
        # run belongs to, which is the one it opened with, not whatever a later event named.
        events = _two_result_stream()

        # Act
        response = await _collect(events)

        # Assert
        assert response.session_id == FIRST_SESSION

    async def test_the_earliest_session_id_also_rides_on_the_exception(self):
        # Arrange
        events = _two_result_stream(last_content="error")

        # Act
        with pytest.raises(AgentExecutionError) as excinfo:
            await _collect(events)

        # Assert
        assert excinfo.value.session_id == FIRST_SESSION


class TestTextIsEveryTextEventInArrivalOrder:
    async def test_text_events_are_newline_joined_in_order(self):
        # Arrange
        events = _two_result_stream()

        # Act
        response = await _collect(events)

        # Assert
        assert response.response == "first answer\nsecond answer"
