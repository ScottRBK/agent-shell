"""NDJSON event fixtures captured from a real OpenCode --format json session."""

STEP_START_EVENT = {
    "type": "step_start",
    "timestamp": 1774816303424,
    "sessionID": "test-session",
    "part": {
        "id": "prt_abc123",
        "messageID": "msg_abc123",
        "sessionID": "test-session",
        "type": "step-start",
    },
}

TEXT_EVENT = {
    "type": "text",
    "timestamp": 1774816303445,
    "sessionID": "test-session",
    "part": {
        "id": "prt_def456",
        "messageID": "msg_abc123",
        "sessionID": "test-session",
        "type": "text",
        "text": "hello world",
        "time": {"start": 1774816303441, "end": 1774816303441},
    },
}

TOOL_USE_EVENT = {
    "type": "tool_use",
    "timestamp": 1774816324549,
    "sessionID": "test-session",
    "part": {
        "id": "prt_ghi789",
        "messageID": "msg_abc123",
        "sessionID": "test-session",
        "type": "tool",
        "tool": "bash",
        "callID": "call_xyz789",
        "state": {
            "status": "completed",
            "input": {"command": "ls", "description": "Lists files"},
            "output": "file1.py\nfile2.py",
            "metadata": {
                "output": "file1.py\nfile2.py",
                "exit": 0,
                "description": "Lists files",
                "truncated": False,
            },
            "title": "Lists files",
            "time": {"start": 1774816324482, "end": 1774816324544},
        },
    },
}

STEP_FINISH_STOP_EVENT = {
    "type": "step_finish",
    "timestamp": 1774816328736,
    "sessionID": "test-session",
    "part": {
        "id": "prt_jkl012",
        "reason": "stop",
        "messageID": "msg_abc123",
        "sessionID": "test-session",
        "type": "step-finish",
        "tokens": {
            "total": 16101,
            "input": 1030,
            "output": 223,
            "reasoning": 0,
            "cache": {"write": 0, "read": 14848},
        },
        "cost": 0.05,
    },
}

STEP_FINISH_TOOL_CALLS_EVENT = {
    "type": "step_finish",
    "timestamp": 1774816324549,
    "sessionID": "test-session",
    "part": {
        "id": "prt_mno345",
        "reason": "tool-calls",
        "messageID": "msg_abc123",
        "sessionID": "test-session",
        "type": "step-finish",
        "tokens": {
            "total": 14973,
            "input": 102,
            "output": 23,
            "reasoning": 0,
            "cache": {"write": 0, "read": 14848},
        },
        "cost": 0.02,
    },
}

def make_step_finish(
    output: int, reason: str, session_id: str = "test-session", reasoning: int = 0
) -> dict:
    """Build a step_finish event carrying per-step output and reasoning token counts.

    OpenCode emits one step_finish per agentic step. Its `tokens.output` is that step's own
    (non-cumulative) output with reasoning ALREADY SUBTRACTED OUT (session.ts:
    `output = outputTokens - reasoningTokens`), and `tokens.reasoning` is the sibling reasoning
    count. Reasoning is billed at the output rate, so a cost-consistent measure sums
    output + reasoning across every step. `reason` is "tool-calls" for work steps and "stop"
    for the terminal step.
    """
    return {
        "type": "step_finish",
        "timestamp": 1774816328736,
        "sessionID": session_id,
        "part": {
            "reason": reason,
            "messageID": "msg_abc123",
            "sessionID": session_id,
            "type": "step-finish",
            "tokens": {
                "total": 100 + output + reasoning,
                "input": 100,
                "output": output,
                "reasoning": reasoning,
                "cache": {"write": 0, "read": 0},
            },
            "cost": 0.01,
        },
    }


ERROR_EVENT = {
    "type": "error",
    "timestamp": 1774816285706,
    "sessionID": "test-session",
    "error": {
        "name": "UnknownError",
        "data": {"message": "Model not found: anthropic/claude-haiku."},
    },
}

UNKNOWN_EVENT = {
    "type": "something_unexpected",
    "timestamp": 1774816300000,
    "sessionID": "test-session",
    "data": "unexpected",
}
