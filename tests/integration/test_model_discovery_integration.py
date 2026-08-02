import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


# Above Linux's maximum pid_max, so test process IDs cannot identify a live process.
MOCK_DISCOVERY_PID = 999_999_999


def _json_rpc_frame(message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


def _make_copilot_process(*messages: dict):
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdin.drain = AsyncMock()
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_data(b"".join(_json_rpc_frame(message) for message in messages))
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = None
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.pid = MOCK_DISCOVERY_PID

    async def wait():
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=wait)
    return process


def _make_completed_process(
    stdout: str,
    *,
    stderr: str = "",
    returncode: int = 0,
):
    process = AsyncMock()
    process.communicate = AsyncMock(
        return_value=(stdout.encode("utf-8"), stderr.encode("utf-8"))
    )
    process.returncode = returncode
    process.kill = MagicMock()
    process.wait = AsyncMock()
    process.pid = MOCK_DISCOVERY_PID
    return process


def _make_empty_stream_process():
    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(return_value=b"")
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.returncode = 0
    process.wait = AsyncMock(return_value=0)
    process.pid = 54321
    return process


def _make_discovery_process(agent_type: AgentType, selector: str):
    if agent_type == AgentType.CLAUDE_CODE:
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "agent-shell-models",
                "response": {"models": [{"value": selector}]},
            },
        }
        return _make_completed_process(json.dumps(response) + "\n")
    if agent_type == AgentType.OPENCODE:
        return _make_completed_process(f"{selector}\n")
    if agent_type == AgentType.COPILOT_CLI:
        return _make_copilot_process(
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"models": [{"id": selector}]},
            },
        )
    if agent_type == AgentType.CODEX:
        catalog = {"models": [{"slug": selector, "visibility": "list"}]}
        return _make_completed_process(json.dumps(catalog))
    if agent_type == AgentType.PI:
        provider, model = selector.split("/", 1)
        return _make_completed_process(
            "provider      model      context  max-out  thinking  images\n"
            f"{provider}  {model}  272K     128K     yes       yes\n"
        )
    return _make_completed_process(
        f"Available models\n\n{selector} - Selected model\n"
    )


class TestModelDiscoveryIntegration:
    @pytest.mark.parametrize(
        "agent_type",
        [
            AgentType.CLAUDE_CODE,
            AgentType.OPENCODE,
            AgentType.CODEX,
            AgentType.PI,
            AgentType.CURSOR,
        ],
    )
    async def test_text_discovery_rejects_non_utf8_output_clearly(self, agent_type):
        # Arrange
        shell = AgentShell(agent_type=agent_type)
        process = _make_completed_process("")
        process.communicate = AsyncMock(return_value=(b"\xff", b""))

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="invalid UTF-8 output"):
                await shell.list_models(cwd="/tmp")

    @pytest.mark.parametrize(
        ("agent_type", "selector", "model_flag"),
        [
            (AgentType.CLAUDE_CODE, "sonnet", "--model"),
            (
                AgentType.OPENCODE,
                "anthropic/claude-sonnet-4-5",
                "-m",
            ),
            (AgentType.COPILOT_CLI, "claude-sonnet-4.5", "--model"),
            (AgentType.CODEX, "gpt-5.4", "--model"),
            (AgentType.PI, "openai-codex/gpt-5.4", "--model"),
            (
                AgentType.CURSOR,
                "claude-opus-4-8[context=1m,effort=high,fast=false]",
                "--model",
            ),
        ],
    )
    async def test_discovered_selector_reaches_the_cli_unchanged(
            self, agent_type, selector, model_flag):
        # Arrange
        shell = AgentShell(agent_type=agent_type)
        discovery_process = _make_discovery_process(agent_type, selector)
        stream_process = _make_empty_stream_process()

        # Act
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=[discovery_process, stream_process],
        ) as mock_exec:
            models = await shell.list_models(cwd="/tmp")
            async for _ in shell.stream(
                cwd="/tmp",
                prompt="test",
                model=models[0],
            ):
                pass

        # Assert
        assert models == [selector]
        stream_command = mock_exec.call_args_list[-1].args
        model_index = stream_command.index(model_flag)
        assert stream_command[model_index + 1] == selector

    async def test_opencode_returns_exact_selectable_model_strings(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        process = _make_completed_process(
            "anthropic/claude-sonnet-4-5\ncustom provider/qwen-3\n"
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == [
            "anthropic/claude-sonnet-4-5",
            "custom provider/qwen-3",
        ]
        assert mock_exec.call_args.args[:2] == ("opencode", "models")
        assert mock_exec.call_args.kwargs["cwd"] == "/tmp"
        assert mock_exec.call_args.kwargs["env"]["PWD"] == "/tmp"

    async def test_discovery_timeout_raises_an_actionable_error(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        process = _make_completed_process("")
        process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        process.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        process.returncode = None

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process), patch(
            "agent_shell.adapters.model_discovery.kill_process_group"
        ) as mock_kill_group:
            with pytest.raises(RuntimeError, match="opencode models.*timed out"):
                await shell.list_models(cwd="/tmp", timeout=0.01)
        mock_kill_group.assert_called_once_with(process)

    async def test_discovery_kills_the_process_on_keyboard_interrupt(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.OPENCODE)
        process = _make_completed_process("")
        process.communicate = AsyncMock(side_effect=KeyboardInterrupt)
        process.returncode = None

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process), patch(
            "agent_shell.adapters.model_discovery.kill_process_group"
        ) as mock_kill_group:
            with pytest.raises(KeyboardInterrupt):
                await shell.list_models(cwd="/tmp")
        mock_kill_group.assert_called_once_with(process)

    async def test_cursor_returns_exact_selectable_model_strings(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        process = _make_completed_process(
            "Available models\n\n"
            "auto - Auto (current, default)\n"
            "claude-opus-4-8[context=1m,effort=high,fast=false] - Claude Opus\n\n"
            "Tip: use --model <id> (or /model <id> in interactive mode) to switch.\n"
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == [
            "auto",
            "claude-opus-4-8[context=1m,effort=high,fast=false]",
        ]
        assert mock_exec.call_args.args[:2] == ("cursor-agent", "models")

    async def test_cursor_rejects_unexpected_nonempty_output(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CURSOR)
        process = _make_completed_process("Available models\nunknown row format\n")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="Unexpected.*cursor-agent models"):
                await shell.list_models(cwd="/tmp")

    async def test_pi_returns_provider_qualified_model_strings(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_completed_process(
            "provider      model                   context  max-out  thinking  images\n"
            "openai-codex  gpt-5.4-mini            272K     128K     yes       yes\n"
            "custom        organisation/model-v2  128K     32K      no        no\n"
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == [
            "openai-codex/gpt-5.4-mini",
            "custom/organisation/model-v2",
        ]
        assert mock_exec.call_args.args[:3] == (
            "pi",
            "--no-approve",
            "--list-models",
        )

    async def test_pi_returns_empty_list_when_no_models_are_available(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_completed_process(
            "No models available. Configure a provider or credentials.\n"
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process):
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == []

    async def test_pi_rejects_a_partial_catalog_with_loading_warnings(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.PI)
        process = _make_completed_process(
            "provider  model  context  max-out  thinking  images\n"
            "custom    model  128K     32K      no        no\n",
            stderr="Warning: failed to load models.json\n",
        )

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="failed to load models.json"):
                await shell.list_models(cwd="/tmp")

    async def test_codex_returns_picker_visible_model_slugs(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        catalog = {
            "models": [
                {"slug": "gpt-5.4-mini", "visibility": "list"},
                {"slug": "gpt-hidden", "visibility": "hidden"},
                {"slug": "gpt-5.4", "visibility": "list"},
            ]
        }
        process = _make_completed_process(json.dumps(catalog))

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == ["gpt-5.4-mini", "gpt-5.4"]
        assert mock_exec.call_args.args[:3] == ("codex", "debug", "models")

    async def test_codex_rejects_malformed_catalog_json(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        process = _make_completed_process("not JSON")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="invalid JSON"):
                await shell.list_models(cwd="/tmp")

    async def test_codex_rejects_json_without_a_model_catalog(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        process = _make_completed_process('{"unexpected": []}')

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="no model catalog"):
                await shell.list_models(cwd="/tmp")

    async def test_codex_rejects_a_malformed_model_entry(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CODEX)
        process = _make_completed_process('{"models": [null]}')

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="invalid model entry"):
                await shell.list_models(cwd="/tmp")

    async def test_claude_returns_initialization_model_values(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "agent-shell-models",
                "response": {
                    "models": [
                        {"value": "default", "displayName": "Default"},
                        {"value": "sonnet", "displayName": "Sonnet"},
                    ],
                    "account": {"email": "private@example.invalid"},
                },
            },
        }
        process = _make_completed_process(json.dumps(response) + "\n")

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == ["default", "sonnet"]
        command = mock_exec.call_args.args
        assert command[:2] == ("claude", "--print")
        assert "--no-session-persistence" in command
        assert ("--output-format", "stream-json") == command[3:5]
        request = json.loads(process.communicate.await_args.args[0])
        assert request == {
            "request_id": "agent-shell-models",
            "type": "control_request",
            "request": {"subtype": "initialize"},
        }

    async def test_claude_rejects_malformed_output_without_echoing_it(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        process = _make_completed_process("private@example.invalid is not JSON\n")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="invalid JSON") as caught:
                await shell.list_models(cwd="/tmp")
        assert "private@example.invalid" not in str(caught.value)

    async def test_claude_rejects_a_malformed_model_entry(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "agent-shell-models",
                "response": {"models": [None]},
            },
        }
        process = _make_completed_process(json.dumps(response) + "\n")

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="invalid model entry"):
                await shell.list_models(cwd="/tmp")

    async def test_copilot_returns_models_from_headless_json_rpc(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        process = _make_copilot_process(
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "models": [
                        {"id": "auto", "name": "Auto"},
                        {"id": "claude-sonnet", "name": "Claude Sonnet"},
                    ]
                },
            },
        )

        # Act
        with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
            models = await shell.list_models(cwd="/tmp")

        # Assert
        assert models == ["auto", "claude-sonnet"]
        assert mock_exec.call_args.args[:4] == (
            "copilot",
            "--headless",
            "--no-auto-update",
            "--stdio",
        )
        sent = b"".join(call.args[0] for call in process.stdin.write.call_args_list)
        assert b'"method":"connect"' in sent
        assert b'"method":"models.list"' in sent

    async def test_copilot_rejects_a_malformed_model_entry(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        process = _make_copilot_process(
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"models": [None]}},
        )

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(RuntimeError, match="invalid model entry"):
                await shell.list_models(cwd="/tmp")

    async def test_copilot_cleanup_does_not_mask_a_discovery_timeout(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        process = _make_copilot_process()
        process.wait = AsyncMock(side_effect=asyncio.TimeoutError)

        # Act / Assert
        with patch("asyncio.create_subprocess_exec", return_value=process), patch(
            "agent_shell.adapters.model_discovery.kill_process_group"
        ) as mock_kill_group:
            with pytest.raises(RuntimeError, match="timed out after 0.01 seconds"):
                await shell.list_models(cwd="/tmp", timeout=0.01)
        mock_kill_group.assert_called_once_with(process)
        process.terminate.assert_not_called()

    async def test_copilot_cancellation_during_shutdown_kills_the_process_group(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)
        process = _make_copilot_process(
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"models": []}},
        )
        process.wait = AsyncMock(side_effect=asyncio.CancelledError)
        stderr_cancelled = asyncio.Event()
        release_stderr = asyncio.Event()

        async def read_stderr():
            try:
                await release_stderr.wait()
            except asyncio.CancelledError:
                stderr_cancelled.set()
                raise
            return b""

        async def drain_stdin():
            await asyncio.sleep(0)

        process.stderr.read = read_stderr
        process.stdin.drain = AsyncMock(side_effect=drain_stdin)

        # Act / Assert
        try:
            with patch("asyncio.create_subprocess_exec", return_value=process), patch(
                "agent_shell.adapters.model_discovery.kill_process_group"
            ) as mock_kill_group:
                with pytest.raises(asyncio.CancelledError):
                    await shell.list_models(cwd="/tmp")
            mock_kill_group.assert_called_once_with(process)
            assert stderr_cancelled.is_set()
        finally:
            release_stderr.set()
            await asyncio.sleep(0)
