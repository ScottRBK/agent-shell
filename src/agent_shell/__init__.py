"""Public package exports for AgentShell."""

from agent_shell.herdr import (
    HerdrClient,
    HerdrExecutionHost,
    HerdrPane,
    HerdrUnavailableError,
)
from agent_shell.terminal_window import (
    SubprocessTerminalLauncher,
    TerminalWindowExecutionHost,
    TerminalWindowLauncher,
    TerminalWindowRunHandle,
    TerminalWindowUnavailableError,
    discover_terminal_launcher,
)
from agent_shell.interactive import InteractiveConnection
from agent_shell.tmux import TmuxExecutionHost, TmuxPlacement, TmuxTarget, TmuxUnavailableError

__all__ = [
    "HerdrClient",
    "HerdrExecutionHost",
    "HerdrPane",
    "HerdrUnavailableError",
    "InteractiveConnection",
    "SubprocessTerminalLauncher",
    "TerminalWindowExecutionHost",
    "TerminalWindowLauncher",
    "TerminalWindowRunHandle",
    "TerminalWindowUnavailableError",
    "TmuxExecutionHost",
    "TmuxPlacement",
    "TmuxTarget",
    "TmuxUnavailableError",
    "discover_terminal_launcher",
]
