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

# Error run: the assistant message carries stopReason="error" (pi exits 0 here, so
# failure must be detected from agent_end, not from stderr/returncode) AND the reason
# in `errorMessage`. The assistant message below is a verbatim capture of the
# inference-server fault from issue #10 — a model that had stopped loading. The field
# is typed optional on pi's AssistantMessage and pi's json mode emits events
# unfiltered, so it is genuinely on the wire.
AGENT_END_ERROR_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [],
            "api": "openai-completions",
            "provider": "llama-ai-server",
            "model": "qwen3.6-27b-8Q",
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                      "totalTokens": 0,
                      "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                               "total": 0}},
            "stopReason": "error",
            "timestamp": 1781725344003,
            "errorMessage": "500 model name=qwen3.6-27b-8Q failed to load",
        },
    ],
    "willRetry": False,
}

# Same failure shape with `errorMessage` omitted. The field is optional on pi's type,
# so the adapter must still produce something more useful than a bare "error".
AGENT_END_ERROR_NO_MESSAGE_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [],
            "api": "openai-completions",
            "provider": "llama-ai-server",
            "model": "qwen3.6-27b-8Q",
            "usage": {"output": 0, "cost": {"total": 0}},
            "stopReason": "error",
            "timestamp": 1781725344003,
        },
    ],
    "willRetry": False,
}

# Aborted run: pi's StopReason union is stop|length|toolUse|error|aborted. An aborted
# turn did not complete, so it must not report status "ok".
AGENT_END_ABORTED_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "partial"}],
            "api": "openai-completions",
            "provider": "llama-ai-server",
            "model": "qwen3.6-27b-8Q",
            "usage": {"output": 4, "cost": {"total": 0}},
            "stopReason": "aborted",
            "timestamp": 1781725344003,
        },
    ],
    "willRetry": False,
}

# --- auto-retry: TWO agent_end events in ONE process run --------------------
# Auto-retry is on by default in pi (settings-manager.js: `settings.retry?.enabled ?? true`,
# maxRetries 3). A retryable provider fault therefore does not end the run: agent-loop.js
# emits agent_end for the failed attempt with willRetry=true, agent-session.js drops that
# assistant message from agent state, sleeps out the backoff and calls `agent.continue()` —
# which runs another agent loop and emits a SECOND agent_end. Auto-compaction takes the same
# continue path. So one `pi --mode json --print` invocation really can emit more than one
# agent_end, and it is the LAST one that says how the run ended.
#
# agent_end carries `newMessages` — only the messages this loop run created — so the retry's
# agent_end holds just its own assistant message, and neither carries prior session history.
AGENT_END_RETRY_PENDING_EVENT = {
    "type": "agent_end",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [],
            "provider": "llama-ai-server",
            "model": "qwen3.6-27b-8Q",
            "usage": {"output": 0, "cost": {"total": 0.002}},
            "stopReason": "error",
            "errorMessage": "500 Internal Server Error",
        },
    ],
    "willRetry": True,
}

AGENT_END_RETRY_SUCCEEDED_EVENT = {
    "type": "agent_end",
    "messages": [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "PONG"}],
            "provider": "llama-ai-server",
            "model": "qwen3.6-27b-8Q",
            "usage": {"output": 31, "cost": {"total": 0.005}},
            "stopReason": "stop",
        },
    ],
    "willRetry": False,
}

# --- unknown / malformed-shape ---------------------------------------------
UNKNOWN_EVENT = {"type": "some_future_event", "data": {"foo": "bar"}}
