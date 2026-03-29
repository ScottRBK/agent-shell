"""NDJSON event fixtures captured from a real Claude Code stream-json session."""

SYSTEM_EVENT = {
    "type": "system",
    "subtype": "init",
    "session_id": "test-session",
    "model": "claude-opus-4-6[1m]",
}

THINKING_EVENT = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "thinking",
                "thinking": "The user wants me to respond with some text.",
            }
        ]
    },
}

TEXT_EVENT = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "text",
                "text": "Hey! Here's some text output.",
            }
        ]
    },
}

TOOL_USE_EVENT = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_012WnHUKXpN58nFWr42ScvV9",
                "name": "Glob",
                "input": {"pattern": "src/**/*.py"},
            }
        ]
    },
}

USER_TOOL_RESULT_EVENT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "tool_use_id": "toolu_012WnHUKXpN58nFWr42ScvV9",
                "type": "tool_result",
                "content": "src/agent_shell/__init__.py\nsrc/agent_shell/shell.py",
            }
        ],
    },
}

RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "allowed"},
}

MULTI_CONTENT_EVENT = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "text",
                "text": "Let me check that for you.",
            },
            {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": "Read",
                "input": {"file_path": "/tmp/test.py"},
            },
        ]
    },
}

RESULT_EVENT_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 10370,
    "total_cost_usd": 0.16098,
}

RESULT_EVENT_ERROR = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "duration_ms": 5000,
    "total_cost_usd": 0.05,
}

UNKNOWN_EVENT = {
    "type": "something_new",
    "data": "unexpected",
}
