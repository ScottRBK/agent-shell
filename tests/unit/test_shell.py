import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter


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

    def test_raises_for_unsupported_agent(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="Unsupported agent"):
            AgentShell(agent_type=AgentType.CODEX)


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
