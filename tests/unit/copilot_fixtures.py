"""NDJSON event fixtures captured from a real Copilot CLI --output-format=json session."""

MCP_SERVER_STATUS_EVENT = {
    "type": "session.mcp_server_status_changed",
    "data": {"serverName": "forgetful_local", "status": "connected"},
    "id": "954b2564-d2a4-4bbe-97b6-08680ef76a2f",
    "timestamp": "2026-04-18T23:14:47.199Z",
    "parentId": "ac064ea6-4c7f-4fe0-bc76-37fcb36f683b",
    "ephemeral": True,
}

MCP_SERVERS_LOADED_EVENT = {
    "type": "session.mcp_servers_loaded",
    "data": {
        "servers": [
            {"name": "forgetful_local", "status": "connected"},
            {"name": "github-mcp-server", "status": "connected", "source": "builtin"},
        ]
    },
    "id": "1470066d-9a3d-4a9f-a954-4ece6106eaec",
    "timestamp": "2026-04-18T23:14:49.019Z",
    "parentId": "e8ad02ab-07ac-4074-934a-7aafdb0ee412",
    "ephemeral": True,
}

TOOLS_UPDATED_EVENT = {
    "type": "session.tools_updated",
    "data": {"model": "gpt-5.4"},
    "id": "8aeef1f8-a01f-4e96-a0b2-f7a288aefcc3",
    "timestamp": "2026-04-18T23:14:49.955Z",
    "parentId": "7c18d071-ce76-4ecc-a760-861bdbdf82c8",
    "ephemeral": True,
}

USER_MESSAGE_EVENT = {
    "type": "user.message",
    "data": {
        "content": "Return exactly: HELLO_WORLD",
        "transformedContent": "<current_datetime>2026-04-18T23:14:49.957Z</current_datetime>\n\nReturn exactly: HELLO_WORLD\n\n<reminder>\n<sql_tables>No tables currently exist. Default tables (todos, todo_deps) will be created automatically when you first use the SQL tool.</sql_tables>\n</reminder>",
        "attachments": [],
        "interactionId": "798f95a9-bdad-44c9-96e7-ebe5c6e36ff0",
    },
    "id": "f6921d35-e136-4ff1-a8a6-0178fb93fd90",
    "timestamp": "2026-04-18T23:14:49.957Z",
    "parentId": "8aeef1f8-a01f-4e96-a0b2-f7a288aefcc3",
}

TURN_START_EVENT = {
    "type": "assistant.turn_start",
    "data": {
        "turnId": "0",
        "interactionId": "798f95a9-bdad-44c9-96e7-ebe5c6e36ff0",
    },
    "id": "2038efff-d58d-4c68-bf57-77fa2eaa9d55",
    "timestamp": "2026-04-18T23:14:49.959Z",
    "parentId": "f6921d35-e136-4ff1-a8a6-0178fb93fd90",
}

REASONING_DELTA_EVENT = {
    "type": "assistant.reasoning_delta",
    "data": {
        "reasoningId": "a835bc4b-be16-4755-8716-11832e3a24bf",
        "deltaContent": "The user wants me to return exactly 'HELLO_WORLD'.",
    },
    "id": "17a22f93-76d5-426a-8401-fc5864fd6462",
    "timestamp": "2026-04-18T23:15:26.706Z",
    "parentId": "f3711656-8e19-4a08-afab-065438f180c5",
    "ephemeral": True,
}

REASONING_EVENT = {
    "type": "assistant.reasoning",
    "data": {
        "reasoningId": "a835bc4b-be16-4755-8716-11832e3a24bf",
        "content": "**Planning file listing task**\n\nI need to list files in the current directory.",
    },
    "id": "a86297f1-8be2-4660-9975-e543e40c3bb9",
    "timestamp": "2026-04-18T23:15:27.925Z",
    "parentId": "c87e534d-0729-4c73-849f-e82b06f4623a",
    "ephemeral": True,
}

MESSAGE_DELTA_EVENT = {
    "type": "assistant.message_delta",
    "data": {
        "messageId": "34f2015a-70d0-426c-aa7d-96e81fba07f4",
        "deltaContent": "HEL",
    },
    "id": "aa2a7e35-6215-42c3-9938-e27eca6ca547",
    "timestamp": "2026-04-18T23:14:51.091Z",
    "parentId": "ec30f7a2-dc1d-4ee9-a2ef-b035a0129140",
    "ephemeral": True,
}

MESSAGE_DELTA_EVENT_2 = {
    "type": "assistant.message_delta",
    "data": {
        "messageId": "34f2015a-70d0-426c-aa7d-96e81fba07f4",
        "deltaContent": "LO",
    },
    "id": "18f66722-7856-422f-9d4e-260440adfc99",
    "timestamp": "2026-04-18T23:14:51.091Z",
    "parentId": "d272213c-2946-4bca-aad4-d16c1e4c62b9",
    "ephemeral": True,
}

MESSAGE_DELTA_EVENT_3 = {
    "type": "assistant.message_delta",
    "data": {
        "messageId": "34f2015a-70d0-426c-aa7d-96e81fba07f4",
        "deltaContent": "_WORLD",
    },
    "id": "5736be52-8dbb-4796-b0ab-ccb84c22ed32",
    "timestamp": "2026-04-18T23:14:51.091Z",
    "parentId": "3fbdfcc3-f177-4dbf-ac8f-0e002b2e44d1",
    "ephemeral": True,
}

MESSAGE_EVENT_NO_TOOLS = {
    "type": "assistant.message",
    "data": {
        "messageId": "34f2015a-70d0-426c-aa7d-96e81fba07f4",
        "content": "HELLO_WORLD",
        "toolRequests": [],
        "interactionId": "798f95a9-bdad-44c9-96e7-ebe5c6e36ff0",
        "phase": "final_answer",
        "outputTokens": 35,
    },
    "id": "39f684a7-36b6-4377-93cc-07150b543291",
    "timestamp": "2026-04-18T23:14:51.604Z",
    "parentId": "88f85973-8dfd-447a-8010-dba9bbaedcac",
}

MESSAGE_EVENT_WITH_TOOLS = {
    "type": "assistant.message",
    "data": {
        "messageId": "76574897-43de-41df-bf91-ac08a1097d06",
        "content": "I'll grab the first three regular files and save them.",
        "toolRequests": [
            {
                "toolCallId": "call_V5dpqC28CIgUApSB409sp3gf",
                "name": "report_intent",
                "arguments": {"intent": "Writing file list"},
                "type": "function",
            },
            {
                "toolCallId": "call_sKHBE2LZ7EyG5SXpBVCdlbws",
                "name": "bash",
                "arguments": {
                    "command": "cd /tmp && find . -maxdepth 1 -type f -printf '%f\\n' | sort | head -n 3",
                    "description": "List first 3 files and save them",
                    "mode": "sync",
                    "initial_wait": 30,
                },
                "type": "function",
                "intentionSummary": "List first 3 files and save them",
            },
        ],
        "interactionId": "083460d5-14bb-4cc0-b06c-2378a14bffbf",
        "phase": "commentary",
        "outputTokens": 460,
    },
    "id": "c87e534d-0729-4c73-849f-e82b06f4623a",
    "timestamp": "2026-04-18T23:15:27.924Z",
    "parentId": "d685fba7-7f52-47cf-8ad8-a513661328ae",
}

TOOL_EXEC_START_EVENT = {
    "type": "tool.execution_start",
    "data": {
        "toolCallId": "call_sKHBE2LZ7EyG5SXpBVCdlbws",
        "toolName": "bash",
        "arguments": {
            "command": "cd /tmp && find . -maxdepth 1 -type f -printf '%f\\n' | sort | head -n 3 | tee /tmp/filelist.txt",
            "description": "List first 3 files and save them",
            "mode": "sync",
            "initial_wait": 30,
        },
    },
    "id": "8ccbfa42-a65e-46ab-8e5e-6963b9190223",
    "timestamp": "2026-04-18T23:15:27.925Z",
    "parentId": "421227e5-119f-4261-900f-9003ee168082",
}

TOOL_EXEC_COMPLETE_EVENT = {
    "type": "tool.execution_complete",
    "data": {
        "toolCallId": "call_sKHBE2LZ7EyG5SXpBVCdlbws",
        "model": "gpt-5.4",
        "interactionId": "083460d5-14bb-4cc0-b06c-2378a14bffbf",
        "success": True,
        "result": {
            "content": ".5ff77f977c5d5335-00000000.so\n<exited with exit code 0>",
            "detailedContent": ".5ff77f977c5d5335-00000000.so\n<exited with exit code 0>",
        },
        "toolTelemetry": {
            "properties": {"customTimeout": "true", "executionMode": "sync"},
            "metrics": {"commandTimeout": 30000},
        },
    },
    "id": "fe967dc5-486e-4a5d-8ed6-746c6899d9f7",
    "timestamp": "2026-04-18T23:15:28.467Z",
    "parentId": "56b75960-857e-4bfd-b87c-b9104104d90a",
}

TURN_END_EVENT = {
    "type": "assistant.turn_end",
    "data": {"turnId": "0"},
    "id": "46cfed53-cda6-41a6-9258-97b1041680fc",
    "timestamp": "2026-04-18T23:14:51.604Z",
    "parentId": "39f684a7-36b6-4377-93cc-07150b543291",
}

RESULT_EVENT_SUCCESS = {
    "type": "result",
    "timestamp": "2026-04-18T23:14:51.605Z",
    "sessionId": "01036873-9931-4e3e-b3cb-14793ae370f9",
    "exitCode": 0,
    "usage": {
        "premiumRequests": 1,
        "totalApiDurationMs": 1138,
        "sessionDurationMs": 4454,
        "codeChanges": {
            "linesAdded": 0,
            "linesRemoved": 0,
            "filesModified": [],
        },
    },
}

RESULT_EVENT_SUCCESS_WITH_USAGE = {
    "type": "result",
    "timestamp": "2026-04-18T23:15:31.773Z",
    "sessionId": "e9a5f1b2-52cd-4966-9cf3-826579fc5020",
    "exitCode": 0,
    "usage": {
        "premiumRequests": 1,
        "totalApiDurationMs": 14397,
        "sessionDurationMs": 14779,
        "codeChanges": {
            "linesAdded": 0,
            "linesRemoved": 0,
            "filesModified": [],
        },
    },
}

RESULT_EVENT_ERROR = {
    "type": "result",
    "timestamp": "2026-04-18T23:14:51.605Z",
    "sessionId": "01036873-9931-4e3e-b3cb-14793ae370f9",
    "exitCode": 1,
    "usage": {
        "premiumRequests": 1,
        "totalApiDurationMs": 5000,
        "sessionDurationMs": 5000,
        "codeChanges": {
            "linesAdded": 0,
            "linesRemoved": 0,
            "filesModified": [],
        },
    },
}

BACKGROUND_TASKS_CHANGED_EVENT = {
    "type": "session.background_tasks_changed",
    "data": {},
    "id": "c8ecb320-7cce-4dca-a1c8-c891a81a6f0f",
    "timestamp": "2026-04-18T23:15:28.162Z",
    "parentId": "474e8bfc-7d20-4796-8fde-811c3d7d7aa9",
    "ephemeral": True,
}

UNKNOWN_EVENT = {
    "type": "session.unknown_internal_event",
    "data": {"foo": "bar"},
    "id": "unknown-event-id",
    "timestamp": "2026-04-18T23:14:47.000Z",
    "ephemeral": True,
}
