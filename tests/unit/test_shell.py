import pytest

from agent_shell import shell as shell_module
from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter
from agent_shell.adapters.grok_adapter import GrokAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.pi_adapter import PiAdapter
from agent_shell.execution import (
    LinuxPidNamespaceIsolation,
    NativeExecutionHost,
    NoIsolation,
)
from agent_shell.models.agent import AgentResponse, AgentType
from agent_shell.shell import AgentShell


class TestExecutionDefaults:
    def test_environment_can_select_linux_pid_namespace_isolation(self, monkeypatch):
        # Arrange
        monkeypatch.setenv(
            "AGENTSHELL_ISOLATION_POLICY",
            "linux-pid-namespace",
        )

        # Act
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Assert
        assert isinstance(shell.isolation_policy, LinuxPidNamespaceIsolation)

    @pytest.mark.parametrize("value", ["", "container"])
    def test_invalid_environment_is_rejected_instead_of_disabling_isolation(
            self, monkeypatch, value):
        # Arrange
        monkeypatch.setenv("AGENTSHELL_ISOLATION_POLICY", value)

        # Act / Assert
        with pytest.raises(
            ValueError,
            match="AGENTSHELL_ISOLATION_POLICY",
        ):
            AgentShell(agent_type=AgentType.CLAUDE_CODE)

    def test_environment_can_explicitly_select_no_isolation(self, monkeypatch):
        # Arrange
        monkeypatch.setenv("AGENTSHELL_ISOLATION_POLICY", "none")

        # Act
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Assert
        assert isinstance(shell.isolation_policy, NoIsolation)

    def test_existing_constructor_defaults_to_native_without_isolation(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Assert
        assert isinstance(shell.execution_host, NativeExecutionHost)
        assert isinstance(shell.isolation_policy, NoIsolation)

    def test_explicit_execution_choices_are_retained(self):
        # Arrange
        host = NativeExecutionHost()
        policy = NoIsolation()

        # Act
        shell = AgentShell(
            agent_type=AgentType.CLAUDE_CODE,
            execution_host=host,
            isolation_policy=policy,
        )

        # Assert
        assert shell.execution_host is host
        assert shell.isolation_policy is policy

    def test_explicit_isolation_policy_overrides_environment(self, monkeypatch):
        # Arrange
        monkeypatch.setenv(
            "AGENTSHELL_ISOLATION_POLICY",
            "linux-pid-namespace",
        )
        policy = NoIsolation()

        # Act
        shell = AgentShell(
            agent_type=AgentType.CLAUDE_CODE,
            isolation_policy=policy,
        )

        # Assert
        assert shell.isolation_policy is policy


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

    def test_resolves_grok(self):
        # Arrange / Act
        shell = AgentShell(agent_type=AgentType.GROK)

        # Assert
        assert isinstance(shell._adapter, GrokAdapter)

    def test_raises_for_agent_type_with_no_registered_adapter(self, monkeypatch):
        # Arrange — every AgentType now has an adapter, so the guard is unreachable through
        # the enum as it stands. Dropping an entry reproduces the mistake the guard exists
        # to catch: a new AgentType member added without a registry entry.
        registry = dict(shell_module._ADAPTERS)
        del registry[AgentType.CURSOR]
        monkeypatch.setattr(shell_module, "_ADAPTERS", registry)

        # Act / Assert
        with pytest.raises(ValueError, match="Unsupported agent"):
            AgentShell(agent_type=AgentType.CURSOR)

    def test_registry_covers_every_agent_type(self):
        # Arrange / Act / Assert — the guard above is a safety net, not a plan. Nothing
        # shipped should ever hit it, so pin the registry to the enum.
        assert set(shell_module._ADAPTERS) == set(AgentType)


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

    async def test_list_models_raises_for_nonexistent_cwd(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)

        # Act / Assert
        with pytest.raises(ValueError, match="Directory does not exist"):
            await shell.list_models(cwd="/nonexistent/path")


class TestModelDiscoveryForwarding:
    async def test_list_models_forwards_cwd_and_timeout(self):
        # Arrange
        shell = AgentShell(agent_type=AgentType.CLAUDE_CODE)
        recorded: dict = {}

        async def fake_list_models(**kwargs):
            recorded.update(kwargs)
            return ["sonnet"]

        shell._adapter.list_models = fake_list_models

        # Act
        models = await shell.list_models(cwd="/tmp", timeout=12.5)

        # Assert
        assert models == ["sonnet"]
        assert recorded == {"cwd": "/tmp", "timeout": 12.5}


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
