"""NDJSON event fixtures captured from real `codex exec --json` runs."""

THREAD_STARTED_EVENT = {
    "type": "thread.started",
    "thread_id": "019e115b-8594-7393-8ed4-bd6cf6127f2a",
}

TURN_STARTED_EVENT = {"type": "turn.started"}

AGENT_MESSAGE_COMPLETED_EVENT = {
    "type": "item.completed",
    "item": {
        "id": "item_0",
        "type": "agent_message",
        "text": "PONG",
    },
}

AGENT_MESSAGE_COMPLETED_LONG_EVENT = {
    "type": "item.completed",
    "item": {
        "id": "item_0",
        "type": "agent_message",
        "text": "one calm\ntwo bright\nthree steady\nfour crisp\nfive warm",
    },
}

AGENT_MESSAGE_COMPLETED_SECOND_EVENT = {
    "type": "item.completed",
    "item": {
        "id": "item_2",
        "type": "agent_message",
        "text": "`hi-from-tool-call`",
    },
}

COMMAND_EXECUTION_STARTED_EVENT = {
    "type": "item.started",
    "item": {
        "id": "item_1",
        "type": "command_execution",
        "command": "/bin/bash -lc 'echo hi-from-tool-call'",
        "aggregated_output": "",
        "exit_code": None,
        "status": "in_progress",
    },
}

COMMAND_EXECUTION_COMPLETED_EVENT = {
    "type": "item.completed",
    "item": {
        "id": "item_1",
        "type": "command_execution",
        "command": "/bin/bash -lc 'echo hi-from-tool-call'",
        "aggregated_output": "hi-from-tool-call\n",
        "exit_code": 0,
        "status": "completed",
    },
}

TURN_COMPLETED_EVENT = {
    "type": "turn.completed",
    "usage": {
        "input_tokens": 30413,
        "cached_input_tokens": 2432,
        "output_tokens": 22,
        "reasoning_output_tokens": 14,
    },
}

# Codex reports run failure on stdout via turn.failed (the real reason lives here),
# even though the process also exits non-zero with only "Reading additional input
# from stdin..." on stderr. Captured from a bad-model run.
TURN_FAILED_EVENT = {
    "type": "turn.failed",
    "error": {"message": "The 'bogus-model' model is not supported with your account."},
}

UNKNOWN_EVENT = {
    "type": "session.unknown_internal_event",
    "data": {"foo": "bar"},
}

MCP_LIST_OUTPUT = """[
  {
    "name": "agentshell_spike_http",
    "enabled": true,
    "disabled_reason": null,
    "transport": {
      "type": "streamable_http",
      "url": "https://example.com/mcp",
      "bearer_token_env_var": "MY_TOKEN",
      "http_headers": null,
      "env_http_headers": null
    },
    "startup_timeout_sec": null,
    "tool_timeout_sec": null,
    "auth_status": "bearer_token"
  },
  {
    "name": "agentshell_spike_stdio",
    "enabled": true,
    "disabled_reason": null,
    "transport": {
      "type": "stdio",
      "command": "/usr/bin/echo",
      "args": ["hello", "world"],
      "env": {"FOO": "bar", "BAZ": "qux"},
      "env_vars": [],
      "cwd": null
    },
    "startup_timeout_sec": null,
    "tool_timeout_sec": null,
    "auth_status": "unsupported"
  }
]
"""

MCP_LIST_EMPTY = "[]\n"
