"""Existing public imports remain valid when contracts move into ports."""

from importlib import import_module

import pytest


@pytest.mark.parametrize(("public_module", "port_module", "names"), [
    (
        "agent_shell.adapters.agent_adapter_protocol",
        "agent_shell.ports.agent",
        ["AgentAdapter"],
    ),
    (
        "agent_shell.execution",
        "agent_shell.ports.execution",
        ["RunHandle", "ExecutionHost", "IsolationPolicy", "IsolationUnavailableError",
         "PreparedLaunch"],
    ),
    (
        "agent_shell.interactive",
        "agent_shell.ports.interactive",
        ["TerminalControl", "InteractiveTerminal", "InteractiveAttachmentHost",
         "InteractiveExecutionHost", "InteractiveLaunch", "InteractiveAdapter"],
    ),
])
def test_existing_public_imports_reexport_the_same_ports(public_module, port_module, names):
    # Arrange
    public = import_module(public_module)

    # Act
    ports = import_module(port_module)

    # Assert
    for name in names:
        assert getattr(public, name) is getattr(ports, name)
