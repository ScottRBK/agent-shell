"""Model-discovery E2E smoke tests — real CLIs, no inference calls or API spend."""

import pytest

from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


pytestmark = pytest.mark.e2e


AGENT_TYPES = [
    pytest.param(AgentType.CLAUDE_CODE, id="claude"),
    pytest.param(AgentType.OPENCODE, id="opencode"),
    pytest.param(AgentType.COPILOT_CLI, id="copilot"),
    pytest.param(AgentType.CODEX, id="codex"),
    pytest.param(AgentType.PI, id="pi"),
    pytest.param(AgentType.CURSOR, id="cursor"),
    pytest.param(AgentType.GROK, id="grok"),
]


@pytest.mark.parametrize("agent_type", AGENT_TYPES)
async def test_real_cli_returns_selectable_model_strings(agent_type):
    # Arrange
    shell = AgentShell(agent_type=agent_type)

    # Act
    models = await shell.list_models(cwd="/tmp")

    # Assert
    assert models, f"{agent_type} returned no selectable models"
    assert all(isinstance(model, str) and model for model in models)
