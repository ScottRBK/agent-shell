"""NDJSON event fixtures captured from real `pi --mode json --print` runs.

Captured against pi 0.79.10 (provider llama-ai-server, model qwen3.6-27b-8Q).
`partial` payloads on the streaming deltas are trimmed to the fields the parser
reads; the lifecycle and `_end` events are kept faithful to the real shape.
"""

# --- session ---------------------------------------------------------------
SESSION_EVENT = {
    "type": "session",
    "version": 3,
    "id": "019f0ae6-995e-780b-b2e7-f00d2d72873f",
    "timestamp": "2026-06-27T21:05:12.542Z",
    "cwd": "/tmp/agentshell-pi",
}

# --- lifecycle noise (all skipped) -----------------------------------------
AGENT_START_EVENT = {"type": "agent_start"}
TURN_START_EVENT = {"type": "turn_start"}

MESSAGE_START_USER_EVENT = {
    "type": "message_start",
    "message": {
        "role": "user",
        "content": [{"type": "text", "text": "Reply with exactly the one word: PONG."}],
        "timestamp": 1782594312566,
    },
}

MESSAGE_END_USER_EVENT = {
    "type": "message_end",
    "message": {
        "role": "user",
        "content": [{"type": "text", "text": "Reply with exactly the one word: PONG."}],
        "timestamp": 1782594312566,
    },
}

# An assistant message_end also carries usage, but the result is built from
# agent_end (which aggregates every turn), so this is skipped.
MESSAGE_END_ASSISTANT_EVENT = {
    "type": "message_end",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "PONG"}],
        "usage": {"input": 1956, "output": 27, "totalTokens": 1983,
                  "cost": {"input": 0, "output": 0, "total": 0}},
        "stopReason": "stop",
    },
}

TURN_END_EVENT = {
    "type": "turn_end",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "PONG"}],
        "usage": {"output": 27, "cost": {"total": 0}},
        "stopReason": "stop",
    },
    "toolResults": [],
}

# --- streaming deltas (skipped: text/thinking are surfaced on the _end) -----
THINKING_START_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
}

THINKING_DELTA_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "The"},
}

TEXT_START_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "text_start", "contentIndex": 1},
}

TEXT_DELTA_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1, "delta": "P"},
}

# --- the _end carriers we DO surface ---------------------------------------
THINKING_END_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {
        "type": "thinking_end",
        "contentIndex": 0,
        "content": "The user wants me to reply with exactly one word: PONG.\n",
    },
}

TEXT_END_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "text_end", "contentIndex": 1, "content": "PONG"},
}

TEXT_END_EMPTY_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "text_end", "contentIndex": 1, "content": ""},
}

THINKING_END_EMPTY_UPDATE = {
    "type": "message_update",
    "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0, "content": ""},
}

# --- tool execution --------------------------------------------------------
TOOL_EXECUTION_START_EVENT = {
    "type": "tool_execution_start",
    "toolCallId": "O8RpULbms21oLKhNjUA4nKiWfoHAK43G",
    "toolName": "bash",
    "args": {"command": "echo HELLO"},
}

TOOL_EXECUTION_UPDATE_EVENT = {
    "type": "tool_execution_update",
    "toolCallId": "O8RpULbms21oLKhNjUA4nKiWfoHAK43G",
    "toolName": "bash",
    "args": {"command": "echo HELLO"},
    "partialResult": {"content": []},
}

TOOL_EXECUTION_END_EVENT = {
    "type": "tool_execution_end",
    "toolCallId": "O8RpULbms21oLKhNjUA4nKiWfoHAK43G",
    "toolName": "bash",
    "result": {"content": [{"type": "text", "text": "HELLO\n"}]},
    "isError": False,
}

# --- agent_end: the result carrier -----------------------------------------
# Single-turn text run. Σ usage.output over assistant messages = 27.
AGENT_END_TEXT_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user",
         "content": [{"type": "text", "text": "Reply with exactly the one word: PONG."}]},
        {"role": "assistant",
         "content": [
             {"type": "thinking", "thinking": "...", "thinkingSignature": "reasoning_content"},
             {"type": "text", "text": "PONG"},
         ],
         "usage": {"input": 1956, "output": 27, "totalTokens": 1983,
                   "cost": {"input": 0, "output": 0, "total": 0}},
         "stopReason": "stop"},
    ],
    "willRetry": False,
}

# Tool-use run: two assistant turns. Σ usage.output = 47 + 8 = 55.
AGENT_END_TOOLUSE_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user",
         "content": [{"type": "text", "text": "Use your bash tool to run: echo HELLO"}]},
        {"role": "assistant",
         "content": [
             {"type": "thinking", "thinking": "...", "thinkingSignature": "reasoning_content"},
             {"type": "toolCall", "toolName": "bash"},
         ],
         "usage": {"output": 47, "cost": {"total": 0}},
         "stopReason": "toolUse"},
        {"role": "toolResult", "content": [{"type": "text", "text": "HELLO\n"}]},
        {"role": "assistant",
         "content": [
             {"type": "thinking", "thinking": "...", "thinkingSignature": "reasoning_content"},
             {"type": "text", "text": "DONE"},
         ],
         "usage": {"output": 8, "cost": {"total": 0}},
         "stopReason": "stop"},
    ],
    "willRetry": False,
}

# Error run: the assistant message carries stopReason="error" (pi exits 0 here,
# so failure must be detected from agent_end, not from stderr/returncode).
AGENT_END_ERROR_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [], "stopReason": "error"},
    ],
    "willRetry": False,
}

# --- unknown / malformed-shape ---------------------------------------------
UNKNOWN_EVENT = {"type": "some_future_event", "data": {"foo": "bar"}}
