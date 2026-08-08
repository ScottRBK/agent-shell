"""streaming-messages-json fixtures captured from real `grok -p` runs.

Captured against grok 1.0.0 (3cd0d0cbce) [stable], model grok-4.5 / grok-4.5-build.
This wire format is Claude/Cursor-shaped (full assistant blocks, not token deltas), which
is why the adapter uses it instead of `streaming-json` — execute() joins text events with
newlines and delta text would explode replies (same class of bug as issue #6).
"""

SESSION_ID = "019fe300-7670-7b73-8100-8217dfb68c38"

# --- system/init: the session-id carrier -----------------------------------
SYSTEM_INIT_EVENT = {
    "type": "system",
    "subtype": "init",
    "session_id": SESSION_ID,
    "apiKeySource": "oauth",
    "model": "grok-4.5",
    "cwd": "/tmp",
    "permissionMode": "bypassPermissions",
    # Real init lists the shell tool as run_terminal_command; --disallowed-tools still
    # wants the shorter run_terminal_cmd id (verified live on grok 1.0.0).
    "tools": [
        "run_terminal_command", "read_file", "search_replace", "list_dir", "grep",
        "write", "web_search", "web_fetch",
    ],
}

SYSTEM_INIT_NO_SESSION_EVENT = {
    "type": "system",
    "subtype": "init",
    "apiKeySource": "oauth",
    "model": "grok-4.5",
    "permissionMode": "bypassPermissions",
}

# --- user echo / tool results (skipped) ------------------------------------
USER_EVENT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "Reply PONG"}]},
    "session_id": SESSION_ID,
}

# --- assistant: full content blocks ----------------------------------------
ASSISTANT_TEXT_EVENT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "PONG"}],
    },
    "session_id": SESSION_ID,
}

ASSISTANT_TEXT_EMPTY_EVENT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": ""}],
    },
    "session_id": SESSION_ID,
}

ASSISTANT_THINKING_AND_TEXT_EVENT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "The user wants me to reply with exactly the word PONG.",
            },
            {"type": "text", "text": "PONG"},
        ],
    },
    "session_id": SESSION_ID,
}

ASSISTANT_TOOL_USE_EVENT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Need to list /tmp."},
            {
                "type": "tool_use",
                "id": "call-c9c8d652-afb5-4986-85dd-594c8b20c0a2-0",
                "name": "list_dir",
                "input": {"target_directory": "/tmp"},
            },
        ],
    },
    "session_id": SESSION_ID,
}

ASSISTANT_SERVER_TOOL_USE_EVENT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "server_tool_use",
                "id": "srv-1",
                "name": "web_search",
                "input": {"query": "agentshell"},
            },
        ],
    },
    "session_id": SESSION_ID,
}

# --- result: the terminal carrier ------------------------------------------
RESULT_SUCCESS_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 4034,
    "duration_api_ms": 2259,
    "num_turns": 1,
    "result": "PONG",
    "stop_reason": "end_turn",
    "total_cost_usd": 0.0359528,
    "usage": {
        "input_tokens": 17761,
        "output_tokens": 27,
        "cache_read_input_tokens": 896,
        "cache_creation_input_tokens": 0,
        # Subset of output_tokens when present — do NOT add into billed output.
        "reasoning_tokens": 5,
    },
    "session_id": SESSION_ID,
}

RESULT_ERROR_EVENT = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "duration_ms": 1200,
    "result": "",
    "stop_reason": "error",
    "total_cost_usd": 0.001,
    "usage": {"output_tokens": 5, "reasoning_tokens": 2},
    "errors": [{"message": "model overloaded"}],
    "session_id": SESSION_ID,
}

RESULT_NO_USAGE_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 500,
    "result": "PONG",
    "session_id": SESSION_ID,
}

UNKNOWN_EVENT = {"type": "some_future_event", "data": {"foo": "bar"}}
