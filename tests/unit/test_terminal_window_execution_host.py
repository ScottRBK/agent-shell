"""Unit coverage for launcher selection/configuration."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_shell.execution import (
    SubprocessTerminalLauncher,
    TerminalWindowUnavailableError,
    discover_terminal_launcher,
)


def test_discover_uses_configured_launcher_without_shell_parsing(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENTSHELL_TERMINAL_LAUNCHER", "custom-terminal")
    monkeypatch.setattr(
        "agent_shell.execution.shutil.which",
        lambda name: "/opt/bin/custom-terminal" if name == "custom-terminal" else None,
    )

    # Act
    launcher = discover_terminal_launcher()

    # Assert
    assert launcher.executable == "/opt/bin/custom-terminal"
    assert launcher.command_prefix == ()
    assert launcher.command_option == "-e"
    assert launcher.display == "any"


def test_discover_reports_an_empty_configured_launcher(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENTSHELL_TERMINAL_LAUNCHER", "  ")

    # Act / Assert
    with pytest.raises(TerminalWindowUnavailableError, match="is empty"):
        discover_terminal_launcher()


def test_discover_reports_no_supported_launcher(monkeypatch):
    # Arrange
    monkeypatch.delenv("AGENTSHELL_TERMINAL_LAUNCHER", raising=False)
    monkeypatch.setattr("agent_shell.execution.shutil.which", lambda _name: None)

    # Act / Assert
    with pytest.raises(TerminalWindowUnavailableError, match="no supported terminal launcher"):
        discover_terminal_launcher()


async def test_subprocess_launcher_keeps_worker_arguments_separate():
    # Arrange
    launcher = SubprocessTerminalLauncher(
        "/opt/bin/terminal",
        command_prefix=("--profile", "default"),
        command_option="--",
    )
    process = object()

    # Act
    with patch(
        "agent_shell.execution.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ) as create:
        result = await launcher.launch(
            ["/usr/bin/python", "-c", "print('worker')"],
            cwd="/tmp",
            env={"DISPLAY": ":0"},
        )

    # Assert
    assert result is process
    assert create.call_args.args == (
        "/opt/bin/terminal",
        "--profile",
        "default",
        "--",
        "/usr/bin/python",
        "-c",
        "print('worker')",
    )
    assert create.call_args.kwargs["cwd"] == "/tmp"
    assert create.call_args.kwargs["env"] == {"DISPLAY": ":0"}
