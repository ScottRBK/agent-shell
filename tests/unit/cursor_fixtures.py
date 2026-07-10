"""stream-json event fixtures captured from real `cursor-agent --print` runs.

Captured against cursor-agent 2026.07.09-a3815c0 (Free plan, model Auto). The
large `shellToolCall`/`mcpToolCall` payloads are trimmed to the fields the parser
reads; the lifecycle/result events are kept faithful to the real shape.
"""

SESSION_ID = "9d179f27-c5c8-4e84-9523-928722eb17ae"

# --- system/init: the session-id carrier -----------------------------------
SYSTEM_INIT_EVENT = {
    "type": "system",
    "subtype": "init",
    "apiKeySource": "login",
    "cwd": "/tmp/agentshell-cursor",
    "session_id": SESSION_ID,
    "model": "Auto",
    "permissionMode": "default",
}

# init without a session_id — nothing to carry, so nothing is emitted.
SYSTEM_INIT_NO_SESSION_EVENT = {
    "type": "system",
    "subtype": "init",
    "apiKeySource": "login",
    "model": "Auto",
    "permissionMode": "default",
}

# --- user echo (skipped) ----------------------------------------------------
USER_EVENT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "Reply PONG"}]},
    "session_id": SESSION_ID,
}

# --- thinking: deltas carry text, completed does not ------------------------
THINKING_DELTA_EVENT = {
    "type": "thinking",
    "subtype": "delta",
    "text": "The user wants me to reply PONG.",
    "session_id": SESSION_ID,
    "timestamp_ms": 1783694004649,
}

THINKING_DELTA_EMPTY_EVENT = {
    "type": "thinking",
    "subtype": "delta",
    "text": "",
    "session_id": SESSION_ID,
    "timestamp_ms": 1783694004650,
}

# The completed carrier has NO text field (thinking text is only in the deltas).
THINKING_COMPLETED_EVENT = {
    "type": "thinking",
    "subtype": "completed",
    "session_id": SESSION_ID,
    "timestamp_ms": 1783694006012,
}

# --- assistant: full text blocks (not deltas) -------------------------------
ASSISTANT_TEXT_EVENT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "PONG"}]},
    "session_id": SESSION_ID,
}

ASSISTANT_TEXT_EMPTY_EVENT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": ""}]},
    "session_id": SESSION_ID,
}

# --- tool_call: shell -------------------------------------------------------
TOOL_CALL_SHELL_STARTED_EVENT = {
    "type": "tool_call",
    "subtype": "started",
    "call_id": "tool_70705751-a970-4075-9c2a-33b8df7c9e6",
    "tool_call": {
        "shellToolCall": {
            "args": {"command": "echo hello-from-shell"},
            "description": "Echo hello-from-shell to stdout",
        }
    },
    "session_id": SESSION_ID,
}

TOOL_CALL_SHELL_COMPLETED_EVENT = {
    "type": "tool_call",
    "subtype": "completed",
    "call_id": "tool_70705751-a970-4075-9c2a-33b8df7c9e6",
    "tool_call": {
        "shellToolCall": {
            "result": {"success": {"exitCode": 0, "stdout": "hello-from-shell\n"}},
        }
    },
    "session_id": SESSION_ID,
}

# --- tool_call: MCP (started carries the name; rejected completion is noise) -
TOOL_CALL_MCP_STARTED_EVENT = {
    "type": "tool_call",
    "subtype": "started",
    "call_id": "tool_0fb563dd-c93f-49b3-b623-1b4c41555c7",
    "tool_call": {
        "mcpToolCall": {
            "args": {
                "name": "plugin-serena-serena-write_memory",
                "toolName": "write_memory",
                "providerIdentifier": "plugin-serena-serena",
            }
        }
    },
    "session_id": SESSION_ID,
}

# Unapproved MCP tools auto-reject headlessly; the completion carries no args.
TOOL_CALL_MCP_REJECTED_EVENT = {
    "type": "tool_call",
    "subtype": "completed",
    "call_id": "tool_0fb563dd-c93f-49b3-b623-1b4c41555c7",
    "tool_call": {
        "mcpToolCall": {
            "result": {"rejected": {"reason": "User rejected MCP: write_memory",
                                    "isReadonly": False}},
        }
    },
    "session_id": SESSION_ID,
}

# --- result: the terminal carrier -------------------------------------------
# usage.outputTokens is undocumented but real; there is no cost field.
RESULT_SUCCESS_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2964,
    "duration_api_ms": 2964,
    "result": "PONG",
    "session_id": SESSION_ID,
    "request_id": "12bc6eac-ea94-4ca1-b875-6e98f7a49abe",
    "usage": {"inputTokens": 98, "outputTokens": 46,
              "cacheReadTokens": 23116, "cacheWriteTokens": 0},
}

# Synthetic error result — captures only ever showed is_error=False (a real
# failure exits non-zero with a plain-text stderr message and no result event),
# so this guards the is_error -> "error" status mapping.
RESULT_ERROR_EVENT = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "duration_ms": 1200,
    "result": "",
    "session_id": SESSION_ID,
    "usage": {"outputTokens": 5},
}

# result with a null/absent usage object — be defensive against a dropped field.
RESULT_NO_USAGE_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 500,
    "result": "PONG",
    "session_id": SESSION_ID,
}

# --- unknown / malformed-shape ----------------------------------------------
UNKNOWN_EVENT = {"type": "some_future_event", "data": {"foo": "bar"}}
