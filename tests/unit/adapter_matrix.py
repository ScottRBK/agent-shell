"""Adapter -> terminal-event lookup for tests that must hold across every adapter.

Each CLI has its own wire format, so a cross-adapter test needs the right NDJSON event per
adapter for whatever it asserts on. The shapes themselves stay in the per-CLI `*_fixtures`
modules (captured from real runs); this only maps adapter class -> shape, so the transport
suite and the execute-outcome suite share one table instead of each growing its own.
"""

import pytest

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.pi_adapter import PiAdapter

from tests.unit.codex_fixtures import TURN_COMPLETED_EVENT
from tests.unit.copilot_fixtures import (
    RESULT_EVENT_ERROR as COPILOT_RESULT_ERROR,
    RESULT_EVENT_SUCCESS as COPILOT_RESULT_SUCCESS,
)
from tests.unit.cursor_fixtures import RESULT_ERROR_EVENT, RESULT_SUCCESS_EVENT
from tests.unit.fixtures import (
    RESULT_EVENT_ERROR as CLAUDE_RESULT_ERROR,
    RESULT_EVENT_SUCCESS as CLAUDE_RESULT_SUCCESS,
)
from tests.unit.opencode_fixtures import STEP_FINISH_STOP_EVENT
from tests.unit.pi_fixtures import AGENT_END_ERROR_EVENT, AGENT_END_TEXT_EVENT

ADAPTERS = [
    pytest.param(ClaudeCodeAdapter, id="claude"),
    pytest.param(CodexAdapter, id="codex"),
    pytest.param(OpenCodeAdapter, id="opencode"),
    pytest.param(CopilotCLIAdapter, id="copilot"),
    pytest.param(PiAdapter, id="pi"),
    pytest.param(CursorAdapter, id="cursor"),
]

# The one NDJSON event each CLI ends a SUCCESSFUL run with — what every adapter normalizes
# into StreamEvent(type="result", content="ok").
OK_RESULT_EVENT = {
    ClaudeCodeAdapter: CLAUDE_RESULT_SUCCESS,
    CodexAdapter: TURN_COMPLETED_EVENT,
    OpenCodeAdapter: STEP_FINISH_STOP_EVENT,
    CopilotCLIAdapter: COPILOT_RESULT_SUCCESS,
    PiAdapter: AGENT_END_TEXT_EVENT,
    CursorAdapter: RESULT_SUCCESS_EVENT,
}

# A terminal result that reports failure — StreamEvent(type="result", content="error").
# Only four CLIs can express it: codex reports a failed turn as a separate `turn.failed`
# and opencode simply never emits its terminal `step_finish`, so both of those reach the
# same normalized failure through an `error` event / a missing result instead.
ERROR_RESULT_EVENT = {
    ClaudeCodeAdapter: CLAUDE_RESULT_ERROR,
    CopilotCLIAdapter: COPILOT_RESULT_ERROR,
    PiAdapter: AGENT_END_ERROR_EVENT,
    CursorAdapter: RESULT_ERROR_EVENT,
}

ERROR_RESULT_ADAPTERS = [
    pytest.param(adapter_cls, id=param.id)
    for param in ADAPTERS
    for adapter_cls in param.values
    if adapter_cls in ERROR_RESULT_EVENT
]
