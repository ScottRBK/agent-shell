"""Public package exports."""

from agent_shell import TmuxExecutionHost, TmuxPlacement
from agent_shell import execution as execution_module
from agent_shell import tmux as tmux_module


def test_tmux_execution_host_is_available_from_the_package_root():
    # Arrange / Act / Assert
    assert TmuxExecutionHost is execution_module.TmuxExecutionHost


def test_tmux_placement_is_available_from_compatibility_exports():
    # Arrange / Act / Assert
    assert TmuxPlacement is tmux_module.TmuxPlacement
    assert TmuxPlacement is execution_module.TmuxPlacement
