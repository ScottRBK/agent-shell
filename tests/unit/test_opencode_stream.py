import json
import os
import warnings
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.models.agent import StreamEvent

from tests.unit.opencode_fixtures import (
    STEP_START_EVENT,
    TEXT_EVENT,
    TOOL_USE_EVENT,
    STEP_FINISH_STOP_EVENT,
    STEP_FINISH_TOOL_CALLS_EVENT,
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
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        assert len(events) == 3
        assert events[0].type == "system"
        assert events[0].session_id == "test-session"
        assert events[1].type == "text"
        assert events[2].type == "result"

    async def test_yields_tool_use_events(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [
            STEP_START_EVENT,
            TOOL_USE_EVENT,
            STEP_FINISH_TOOL_CALLS_EVENT,
            STEP_START_EVENT,
            TEXT_EVENT,
            STEP_FINISH_STOP_EVENT,
        ]
        mock_process = _make_mock_process(ndjson)

        # Act
        events: list[StreamEvent] = []
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            async for event in adapter.stream(cwd="/tmp", prompt="test"):
                events.append(event)

        # Assert
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].content == "bash"

    async def test_yields_error_event_on_nonzero_exit_with_stderr(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT]
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
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", model="anthropic/claude-sonnet-4-5"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-m" in cmd_args
        assert cmd_args[cmd_args.index("-m") + 1] == "anthropic/claude-sonnet-4-5"

    async def test_omits_model_flag_when_none(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-m" not in cmd_args

    async def test_includes_session_flag_in_command(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test", session_id="ses_abc123"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-s" in cmd_args
        assert cmd_args[cmd_args.index("-s") + 1] == "ses_abc123"

    async def test_omits_session_flag_when_none(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert "-s" not in cmd_args

    async def test_prompt_is_last_argument(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            async for _ in adapter.stream(cwd="/tmp", prompt="do something"):
                pass

        # Assert
        cmd_args = mock_exec.call_args[0]
        assert cmd_args[-1] == "do something"

    async def test_skips_malformed_json_lines(self):
        # Arrange
        adapter = OpenCodeAdapter()
        raw = json.dumps(TEXT_EVENT) + "\n" + "not valid json\n" + json.dumps(STEP_FINISH_STOP_EVENT) + "\n"
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


class TestDisallowedTools:
    async def test_execute_with_disallowed_tools_runs(self):
        # Arrange — confirms execute() -> stream() stays wired with the new param.
        adapter = OpenCodeAdapter()
        ndjson = [STEP_START_EVENT, TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_PERMISSION", None)
            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                response = await adapter.execute(
                    cwd="/tmp", prompt="test", disallowed_tools=["bash"]
                )

        # Assert
        assert response.response  # non-empty text means the stream was consumed

    async def test_maps_canonical_to_permission_env(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_PERMISSION", None)
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["bash", "web_search"]
                ):
                    pass

        # Assert
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"bash": "deny", "websearch": "deny"}

    async def test_edit_collapses_to_single_key(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_PERMISSION", None)
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["edit"]
                ):
                    pass

        # Assert
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"edit": "deny"}

    async def test_merges_with_existing_permission(self):
        # Arrange — inherited denies must survive.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(
            os.environ,
            {"OPENCODE_PERMISSION": json.dumps({"bash": "deny", "read": "deny"})},
            clear=False,
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["web_search"]
                ):
                    pass

        # Assert
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"bash": "deny", "read": "deny", "websearch": "deny"}

    async def test_our_deny_wins_over_existing_allow(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(
            os.environ,
            {"OPENCODE_PERMISSION": json.dumps({"websearch": "allow"})},
            clear=False,
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["web_search"]
                ):
                    pass

        # Assert
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"websearch": "deny"}

    async def test_fail_closed_on_invalid_existing_permission(self):
        # Arrange — an unparseable inherited value must not drop our denies.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act / Assert
        with patch.dict(
            os.environ, {"OPENCODE_PERMISSION": "{invalid"}, clear=False
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                with pytest.warns(UserWarning, match="OPENCODE_PERMISSION"):
                    async for _ in adapter.stream(
                        cwd="/tmp", prompt="test", disallowed_tools=["bash"]
                    ):
                        pass

        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"bash": "deny"}

    async def test_inherited_global_deny_string_promoted_to_object_wildcard(self):
        # Arrange — OPENCODE_PERMISSION can be a bare string "deny" (global deny-all). We must
        # honor that intent, but opencode's env-var path drops a bare primitive string (remeda
        # mergeDeep no-op, verified on 1.14.41), so re-emitting "deny" would be silent fail-open.
        # It must instead be promoted to the object wildcard form opencode actually enforces,
        # with our explicit per-tool deny merged on top.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(
            os.environ, {"OPENCODE_PERMISSION": json.dumps("deny")}, clear=False
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["bash"]
                ):
                    pass

        # Assert — object form (not the bare string), carrying both the global wildcard deny
        # and our explicit bash deny.
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"*": "deny", "bash": "deny"}

    async def test_inherited_permissive_scalar_warns_and_applies_our_denies(self):
        # Arrange — a global "allow"/"ask" scalar isn't a per-tool map; apply our denies on an
        # empty base (more restrictive for our tools) and warn that we couldn't merge granularly.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act / Assert
        with patch.dict(
            os.environ, {"OPENCODE_PERMISSION": json.dumps("allow")}, clear=False
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                with pytest.warns(UserWarning, match="OPENCODE_PERMISSION"):
                    async for _ in adapter.stream(
                        cwd="/tmp", prompt="test", disallowed_tools=["bash"]
                    ):
                        pass

        env = mock_exec.call_args.kwargs["env"]
        assert json.loads(env["OPENCODE_PERMISSION"]) == {"bash": "deny"}

    async def test_env_pins_pwd_and_omits_permission_key_when_no_disallowed_tools(self):
        # Arrange
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act — no deny-list, and no inherited OPENCODE_PERMISSION.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_PERMISSION", None)
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                    pass

        # Assert — env is never None now: PWD must be pinned on every run (opencode reads
        # the project dir from $PWD) and the parent env flows through, but with nothing to
        # deny we must not inject a spurious OPENCODE_PERMISSION key.
        env = mock_exec.call_args.kwargs["env"]
        assert env is not None
        assert env["PWD"] == os.path.abspath("/tmp")
        assert "PATH" in env
        assert "OPENCODE_PERMISSION" not in env

    async def test_inherited_bare_deny_promoted_even_without_disallowed_tools(self):
        # Arrange — B2 fix: an inherited bare-string "deny" (global deny-all intent) must be
        # promoted to the {"*": "deny"} object form opencode actually enforces, EVEN when the
        # caller passes no disallowed_tools. Re-forwarding the bare string is a silent no-op under
        # --dangerously-skip-permissions (remeda mergeDeep drops the primitive), so the user's
        # deny-all would fail OPEN. Promotion must not be gated on a caller deny-list being present.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act — note: NO disallowed_tools passed.
        with patch.dict(
            os.environ, {"OPENCODE_PERMISSION": json.dumps("deny")}, clear=False
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                    pass

        # Assert — promoted to the enforceable wildcard object, not the no-op bare string.
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"*": "deny"}

    async def test_inherited_object_permission_preserved_without_disallowed_tools(self):
        # Arrange — regression guard for the B2 refactor: with no caller denies, a valid inherited
        # OBJECT policy must flow through untouched. We only rewrite the known-no-op bare string;
        # we must not drop or mangle a legitimate inherited per-tool map.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act — note: NO disallowed_tools passed.
        with patch.dict(
            os.environ,
            {"OPENCODE_PERMISSION": json.dumps({"bash": "deny"})},
            clear=False,
        ):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(cwd="/tmp", prompt="test"):
                    pass

        # Assert — inherited object survives intact.
        env = mock_exec.call_args.kwargs["env"]
        assert json.loads(env["OPENCODE_PERMISSION"]) == {"bash": "deny"}

    async def test_verbatim_passthrough_name_in_permission_env(self):
        # Arrange — a non-canonical name passes through verbatim as a deny key.
        adapter = OpenCodeAdapter()
        ndjson = [TEXT_EVENT, STEP_FINISH_STOP_EVENT]
        mock_process = _make_mock_process(ndjson)

        # Act
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_PERMISSION", None)
            with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
                async for _ in adapter.stream(
                    cwd="/tmp", prompt="test", disallowed_tools=["mytool"]
                ):
                    pass

        # Assert
        env = mock_exec.call_args.kwargs["env"]
        perm = json.loads(env["OPENCODE_PERMISSION"])
        assert perm == {"mytool": "deny"}
