import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, AgentResponse
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.pi_adapter import PiAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter


class TestResolveAdapter:
    def test_resolves_claude_code(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Assert
        assert isinstance(shell._adapter, ClaudeCodeAdapter)

    def test_resolves_opencode(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.OPENCODE)

        # Assert
        assert isinstance(shell._adapter, OpenCodeAdapter)

    def test_resolves_copilot_cli(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.COPILOT_CLI)

        # Assert
        assert isinstance(shell._adapter, CopilotCLIAdapter)

    def test_resolves_codex(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.CODEX)

        # Assert
        assert isinstance(shell._adapter, CodexAdapter)

    def test_resolves_pi(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.PI)

        # Assert
        assert isinstance(shell._adapter, PiAdapter)

    def test_resolves_cursor(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.CURSOR)

        # Assert
        assert isinstance(shell._adapter, CursorAdapter)

    def test_raises_for_unsupported_agent(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="Unsupported agent"):
            AgentShell(agent_type=AgentType.GEMINI_CLI)


class TestCwdValidation:
    async def test_execute_raises_for_nonexistent_cwd(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act / Assert
        with pytest.raises(ValueError, match="Directory does not exist"):
            await shell.execute(cwd="/nonexistent/path", prompt="test")

    async def test_stream_raises_for_nonexistent_cwd(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act / Assert
        with pytest.raises(ValueError, match="Directory does not exist"):
            async for _ in shell.stream(cwd="/nonexistent/path", prompt="test"):
                pass


class TestDisallowedToolsForwarding:
    async def test_execute_forwards_disallowed_tools_to_adapter(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        recorded: dict = {}

        async def fake_execute(**kwargs):
            recorded.update(kwargs)
            return AgentResponse(response="", cost=0.0)

        shell._adapter.execute = fake_execute

        # Act
        await shell.execute(cwd="/tmp", prompt="hi", disallowed_tools=["bash"])

        # Assert
        assert recorded["disallowed_tools"] == ["bash"]

    async def test_stream_forwards_disallowed_tools_to_adapter(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        recorded: dict = {}

        async def fake_stream(**kwargs):
            recorded.update(kwargs)
            if False:  # pragma: no cover - makes this an async generator
                yield

        shell._adapter.stream = fake_stream

        # Act
        async for _ in shell.stream(cwd="/tmp", prompt="hi", disallowed_tools=["read"]):
            pass

        # Assert
        assert recorded["disallowed_tools"] == ["read"]

    async def test_execute_defaults_disallowed_tools_to_none(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        recorded: dict = {}

        async def fake_execute(**kwargs):
            recorded.update(kwargs)
            return AgentResponse(response="", cost=0.0)

        shell._adapter.execute = fake_execute

        # Act
        await shell.execute(cwd="/tmp", prompt="hi")

        # Assert
        assert recorded["disallowed_tools"] is None

    async def test_positional_args_bind_model_not_disallowed_tools(self):
        # Regression — disallowed_tools must stay LAST in the signature so existing positional
        # callers (`execute(cwd, prompt, allowed_tools, model)`) keep binding `model`, not the
        # new deny-list. Guards against re-inserting the param mid-signature.
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        recorded: dict = {}

        async def fake_execute(**kwargs):
            recorded.update(kwargs)
            return AgentResponse(response="", cost=0.0)

        shell._adapter.execute = fake_execute

        # Act — fourth positional arg is the model.
        await shell.execute("/tmp", "hi", ["Read"], "sonnet")

        # Assert
        assert recorded["allowed_tools"] == ["Read"]
        assert recorded["model"] == "sonnet"
        assert recorded["disallowed_tools"] is None
