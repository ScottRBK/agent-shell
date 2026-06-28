"""Health-check E2E smoke tests — real CLI calls, real (small) API spend.

Local-only (excluded from CI via the `e2e` marker). Confirms `health_check`
correctly reads a real agent: a valid model+combo is healthy, a bogus model is not.
Cheapest model per agent is used to keep spend minimal.
"""

import pytest

from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType, HealthCheckResult


pytestmark = pytest.mark.e2e


# Cheapest known-good model per agent. None => the agent's configured default.
VALID_MODEL = {
    AgentType.CLAUDE_CODE: "haiku",
    AgentType.OPENCODE: "opencode/big-pickle",
    AgentType.CODEX: "gpt-5.4-mini",
    AgentType.COPILOT_CLI: None,
    AgentType.PI: "qwen3.6-27b-8Q",
}

BOGUS_MODEL = "definitely-not-a-real-model-xyz"


class TestHealthyE2E:
    @pytest.mark.parametrize("agent_type", list(VALID_MODEL.keys()))
    async def test_valid_model_is_healthy(self, agent_type):
        # Arrange
        shell = AgentShell(agent_type=agent_type)

        # Act
        result = await shell.health_check(cwd="/tmp", model=VALID_MODEL[agent_type])

        # Assert
        assert isinstance(result, HealthCheckResult)
        assert result.healthy is True, f"{agent_type} unhealthy: {result.exception}"
        assert result.exception is None


class TestUnhealthyE2E:
    @pytest.mark.parametrize("agent_type", list(VALID_MODEL.keys()))
    async def test_bogus_model_is_unhealthy(self, agent_type):
        # Arrange
        shell = AgentShell(agent_type=agent_type)

        # Act
        result = await shell.health_check(cwd="/tmp", model=BOGUS_MODEL)

        # Assert — every CLI's bad-model path must resolve to unhealthy, despite
        # opencode exiting 0 and copilot/pi reporting only on stderr.
        assert result.healthy is False
        assert result.exception is not None
