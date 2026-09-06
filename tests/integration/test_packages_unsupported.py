import pytest

from agent_shell.models.agent import AgentType, PackageSpec
from agent_shell.shell import AgentShell


@pytest.mark.parametrize("agent_type", [agent for agent in AgentType if agent != AgentType.PI])
@pytest.mark.parametrize("operation", ["add_package", "list_packages", "remove_package"])
async def test_unsupported_harness_reports_package_operation(agent_type, operation):
    # Arrange
    shell = AgentShell(agent_type)
    arguments = {
        "add_package": [PackageSpec("npm:tools@1.2.3")],
        "list_packages": [],
        "remove_package": ["npm:tools"],
    }

    # Act / Assert
    with pytest.raises(NotImplementedError, match=operation):
        await getattr(shell, operation)(*arguments[operation])
