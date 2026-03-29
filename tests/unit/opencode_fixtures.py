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
