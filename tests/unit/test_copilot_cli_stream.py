import asyncio
import json
import warnings
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.copilot_fixtures import (
    MESSAGE_DELTA_EVENT,
    MESSAGE_EVENT_NO_TOOLS,
    RESULT_EVENT_SUCCESS,
    make_assistant_message,
)


def _make_mock_process(ndjson_lines: list[dict], returncode: int = 0, stderr: bytes = b""):
    """Create a mock subprocess that yields NDJSON lines from stdout."""
    encoded = "\n".join(json.dumps(line) for line in ndjson_lines) + "\n"
    chunks = [encoded.encode("utf-8"), b""]

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=chunks)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = 12345
    return process


class TestStream:
    async def test_yields_events_in_order(self):
        # Arrange — the leading MESSAGE_DELTA_EVENT is a per-token delta and must be ignored
        # (issue #6); the text event comes from MESSAGE_EVENT_NO_TOOLS's full `content`.
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, MESSAGE_EVENT_NO_TOOLS, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 2
        assert events[0].type == "text"
        assert events[0].content == "HELLO_WORLD"
        assert events[1].type == "result"

    async def test_yields_error_event_on_nonzero_exit_with_stderr(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT]
        mock_process = _make_mock_process(ndjson, returncode=1, stderr=b"something went wrong")

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert events[-1].type == "error"
        assert "something went wrong" in events[-1].content

    async def test_includes_model_flag_in_command(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", model="gpt-4o"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--model" in cmd_args
        assert cmd_args[cmd_args.index("--model") + 1] == "gpt-4o"

    async def test_omits_model_flag_when_none(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--model" not in cmd_args

    async def test_includes_effort_flag_in_command(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", effort="high"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--effort" in cmd_args
        assert cmd_args[cmd_args.index("--effort") + 1] == "high"

    async def test_omits_effort_flag_when_none(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--effort" not in cmd_args

    async def test_includes_allow_all_tools_by_default(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", auto_approve=True):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" in cmd_args

    async def test_omits_allow_all_tools_when_disabled(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", auto_approve=False):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" not in cmd_args

    async def test_includes_allow_tool_for_each_allowed_tool(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", allowed_tools=["Bash", "Read"]
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        allow_tool_indices = [i for i, x in enumerate(cmd_args) if x == "--allow-tool"]
        assert len(allow_tool_indices) == 2
        assert cmd_args[allow_tool_indices[0] + 1] == "Bash"
        assert cmd_args[allow_tool_indices[1] + 1] == "Read"

    async def test_omits_allow_tool_when_none(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", allowed_tools=None):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-tool" not in cmd_args

    async def test_includes_resume_flag_when_session_id_provided(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", session_id="ses_abc123"
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume" in cmd_args
        assert cmd_args[cmd_args.index("--resume") + 1] == "ses_abc123"

    async def test_omits_resume_flag_when_no_session_id(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--resume" not in cmd_args

    async def test_disallowed_bash_maps_to_deny_tool_shell(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", disallowed_tools=["bash"]):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        deny_indices = [i for i, x in enumerate(cmd_args) if x == "--deny-tool"]
        assert len(deny_indices) == 1
        assert cmd_args[deny_indices[0] + 1] == "shell"

    async def test_disallowed_edit_maps_to_single_write_deny(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", disallowed_tools=["edit"]):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        deny_indices = [i for i, x in enumerate(cmd_args) if x == "--deny-tool"]
        assert len(deny_indices) == 1
        assert cmd_args[deny_indices[0] + 1] == "write"
        assert "edit" not in cmd_args

    async def test_unsupported_canonical_warns_and_emits_no_deny_flag(self):
        # Arrange — Copilot has no web_search/web_fetch tools, and `read`'s CLI deny name
        # is unconfirmed, so these canonical names must warn rather than silently no-op.
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="web_search"):
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["read", "web_search", "web_fetch"]
                ):
                    pass

        cmd_args = mock_exec.call_args[0]
        assert "--deny-tool" not in cmd_args

    async def test_supported_and_unsupported_mix_denies_known_and_warns_rest(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act / Assert — bash denied, web_fetch warned.
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with pytest.warns(UserWarning, match="web_fetch"):
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["bash", "web_fetch"]
                ):
                    pass

        cmd_args = mock_exec.call_args[0]
        deny_values = [cmd_args[i + 1] for i, x in enumerate(cmd_args) if x == "--deny-tool"]
        assert deny_values == ["shell"]

    async def test_disallowed_multiple_supported_emit_repeated_deny_flags(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", disallowed_tools=["bash", "edit"]
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        deny_values = [cmd_args[i + 1] for i, x in enumerate(cmd_args) if x == "--deny-tool"]
        assert deny_values == ["shell", "write"]

    async def test_verbatim_passthrough_name_reaches_deny_tool(self):
        # Arrange — a non-canonical name (e.g. Copilot's actual `view` tool, or an MCP
        # `Server(tool)` name) passes through verbatim as the escape hatch.
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", disallowed_tools=["view"]
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        deny_indices = [i for i, x in enumerate(cmd_args) if x == "--deny-tool"]
        assert len(deny_indices) == 1
        assert cmd_args[deny_indices[0] + 1] == "view"

    async def test_omits_deny_tool_when_none(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--deny-tool" not in cmd_args

    async def test_disallowed_coexists_with_allow_all_tools(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(
                cwd="/tmp", prompt="test", auto_approve=True, disallowed_tools=["bash"]
            ):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--allow-all-tools" in cmd_args
        assert "--deny-tool" in cmd_args

    async def test_skips_malformed_json_lines(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        raw = json.dumps(MESSAGE_EVENT_NO_TOOLS) + "\n" + "not valid json\n" + json.dumps(RESULT_EVENT_SUCCESS) + "\n"
        chunks = [raw.encode("utf-8"), b""]

        mock_process = AsyncMock()
        mock_process.stdout = MagicMock()
        mock_process.stdout.read = AsyncMock(side_effect=chunks)
        mock_process.stderr = MagicMock()
        mock_process.stderr.read = AsyncMock(return_value=b"")
        mock_process.returncode = 0
        mock_process.wait = AsyncMock()
        mock_process.pid = 12345

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 2
        assert events[0].type == "text"
        assert events[1].type == "result"

    async def test_base_command_is_copilot_with_flags(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="say hello"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "copilot"
        assert cmd_args[1] == "-p"
        assert cmd_args[2] == "say hello"
        assert "--output-format" in cmd_args
        assert cmd_args[cmd_args.index("--output-format") + 1] == "json"
        assert "--silent" in cmd_args


class TestOutputTokens:
    async def test_result_event_carries_accumulated_output_tokens(self):
        # Arrange — the single result StreamEvent must carry the summed per-message totals.
        # The leading zero-token message yields a text event so D5 (intermediate events == 0)
        # is testable, without perturbing the summed total.
        adapter = CopilotCLIAdapter()
        ndjson = [
            make_assistant_message(0, content="hi"),
            make_assistant_message(618),
            make_assistant_message(71),
            make_assistant_message(201),
            make_assistant_message(36),
            RESULT_EVENT_SUCCESS,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].output_tokens == 926
        # D5: intermediate events never carry the running total.
        assert all(e.output_tokens == 0 for e in events if e.type in ("text", "tool_use"))

    async def test_accumulates_output_tokens_via_eof_buffer_path(self):
        # Arrange — when the final stdout chunk has NO trailing newline, the result event is
        # flushed through the EOF-buffer branch of stream() rather than the newline loop. That
        # branch must accumulate identically (regression guard for the duplicated logic).
        adapter = CopilotCLIAdapter()
        ndjson = [
            make_assistant_message(618),
            make_assistant_message(36),
            RESULT_EVENT_SUCCESS,
        ]
        encoded = "\n".join(json.dumps(line) for line in ndjson)  # NOTE: no trailing newline
        chunks = [encoded.encode("utf-8"), b""]
        mock_process = AsyncMock()
        mock_process.stdout = MagicMock()
        mock_process.stdout.read = AsyncMock(side_effect=chunks)
        mock_process.stderr = MagicMock()
        mock_process.stderr.read = AsyncMock(return_value=b"")
        mock_process.returncode = 0
        mock_process.wait = AsyncMock()
        mock_process.pid = 12345

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert — 618 + 36 == 654, stamped on the result event parsed at EOF.
        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].output_tokens == 654


class TestExecuteForwardsDisallowedTools:
    async def test_execute_forwards_disallowed_tools_to_command(self):
        # Arrange
        adapter = CopilotCLIAdapter()
        ndjson = [MESSAGE_DELTA_EVENT, RESULT_EVENT_SUCCESS]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await adapter.execute(cwd="/tmp", prompt="test", disallowed_tools=["bash"])

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "--deny-tool" in cmd_args
        assert cmd_args[cmd_args.index("--deny-tool") + 1] == "shell"
