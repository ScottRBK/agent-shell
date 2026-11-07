"""Prove dependency contracts through the real CLI, using disposable source copies.

These tests never import the deliberately broken source. Graph caching is disabled for every run.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG_HEADER = '[tool.importlinter]\nroot_package = "agent_shell"\n'


@pytest.fixture
def source_copy(tmp_path):
    # Arrange: no credentials, virtualenv, or generated analysis files enter the fixture.
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _append(root, module, source):
    path = root / "src" / Path(*module.split(".")).with_suffix(".py")
    with path.open("a", encoding="utf-8") as file:
        file.write("\n" + source + "\n")


def _check(root, contract=None, *, enabled=True):
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8") if enabled else CONFIG_HEADER
    path = root / "contracts.toml"
    path.write_text(config, encoding="utf-8")
    selection = ["--contract", contract] if enabled and contract else []
    return subprocess.run(
        [str(Path(sys.executable).with_name("lint-imports")),
         "--config", str(path), "--no-cache", "--no-logo", *selection],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "NO_COLOR": "1", "COLUMNS": "200"},
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("statement", [
    "import agent_shell.adapters.codex_adapter",
    "import agent_shell.adapters.codex_adapter as codex",
    "from agent_shell.adapters.codex_adapter import CodexAdapter as Adapter",
    "from agent_shell.adapters import codex_adapter",
    "from agent_shell.adapters.codex_adapter import *",
    "from .codex_adapter import CodexAdapter",
    "from . import codex_adapter",
    "from ..adapters.codex_adapter import CodexAdapter",
    "def load():\n    from agent_shell.adapters.codex_adapter import CodexAdapter",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import codex_adapter",
])
def test_only_composition_imports_concrete_adapters(source_copy, statement):
    # Arrange
    source = "agent_shell.adapters.health"
    target = "agent_shell.adapters.codex_adapter"
    _append(source_copy, source, statement)

    # Act: a missing rule must permit the violation, not fail for a syntax/import error.
    unchecked = _check(source_copy, "concrete-adapters", enabled=False)
    checked = _check(source_copy, "concrete-adapters")

    # Assert
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "Only composition imports concrete agent adapters BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout


@pytest.mark.parametrize(("source", "target"), [
    ("agent_shell.models.agent", "agent_shell.ports.agent"),
    ("agent_shell.ports.agent", "agent_shell.interactive"),
    ("agent_shell.ports.execution", "agent_shell.execution"),
    ("agent_shell.tmux", "agent_shell.shell"),
])
@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_package_dependencies_point_inward(source_copy, source, target, indirect):
    # Arrange: generated metadata is unlayered, so it also tests an indirect escape route.
    middle = "agent_shell._version"
    if indirect:
        _append(source_copy, source, f"import {middle}")
        _append(source_copy, middle, f"import {target}")
    else:
        _append(source_copy, source, f"import {target}")

    # Act
    unchecked = _check(source_copy, "package-layers", enabled=False)
    checked = _check(source_copy, "package-layers")

    # Assert
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "Package dependencies point inward BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout
    if indirect:
        assert middle in checked.stdout


@pytest.mark.parametrize(("source", "target"), [
    ("agent_shell.adapters.health", "agent_shell.adapters.codex_adapter"),
    ("agent_shell.adapters.codex_adapter", "agent_shell.adapters.claude_code_adapter"),
    ("agent_shell.adapters.agent_adapter_protocol", "agent_shell.adapters.health"),
])
@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_adapter_helpers_stay_below_independent_agents(source_copy, source, target, indirect):
    # Arrange
    middle = "agent_shell._version"
    _append(source_copy, source, f"import {middle if indirect else target}")
    if indirect:
        _append(source_copy, middle, f"import {target}")

    # Act
    unchecked = _check(source_copy, "adapter-layers", enabled=False)
    checked = _check(source_copy, "adapter-layers")

    # Assert
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "Agent adapters use shared helpers, not each other BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout
    if indirect:
        assert middle in checked.stdout


@pytest.mark.parametrize(("source", "target"), [
    ("agent_shell.adapters.health", "agent_shell.execution"),
    ("agent_shell.adapters.response", "agent_shell.adapters.model_discovery"),
    ("agent_shell.adapters.outcome", "agent_shell.shell"),
    ("agent_shell.adapters.agent_adapter_protocol", "agent_shell.tmux"),
    ("agent_shell.interactive", "agent_shell.execution"),
    ("agent_shell.interactive", "agent_shell.adapters.model_discovery"),
])
@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_shared_helpers_cannot_reach_integrations(source_copy, source, target, indirect):
    # Arrange
    middle = "agent_shell._version"
    _append(source_copy, source, f"import {middle if indirect else target}")
    if indirect:
        _append(source_copy, middle, f"import {target}")

    # Act
    unchecked = _check(source_copy, "shared-no-infrastructure", enabled=False)
    checked = _check(source_copy, "shared-no-infrastructure")

    # Assert
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "Shared behaviour does not depend on infrastructure BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout
    if indirect:
        assert middle in checked.stdout


@pytest.mark.parametrize("contract", [
    "concrete-adapters", "package-layers", "adapter-layers", "shared-no-infrastructure", None,
])
def test_real_repository_and_permitted_dependencies_pass(source_copy, contract):
    # Arrange: preserve actual composition/defaults; also exercise extra legitimate port imports.
    _append(source_copy, "agent_shell.ports.agent", "from .execution import RunHandle")
    _append(
        source_copy, "agent_shell.interactive", "from agent_shell.ports.agent import AgentAdapter",
    )
    _append(source_copy, "agent_shell.adapters.health", "from .response import collect_response")
    _append(
        source_copy, "agent_shell.shell",
        "from agent_shell.ports.interactive import TerminalControl",
    )

    # Act
    checked = _check(source_copy, contract)

    # Assert
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "0 broken" in checked.stdout
    assert "KEPT" in checked.stdout


@pytest.mark.parametrize(("source", "statement", "target"), [
    (
        "agent_shell.ports.agent",
        "from agent_shell.execution import ExecutionHost",
        "agent_shell.execution",
    ),
    (
        "agent_shell.interactive",
        "from agent_shell.shell import CodexAdapter",
        "agent_shell.shell",
    ),
    (
        "agent_shell.adapters.health",
        "from agent_shell import TmuxExecutionHost",
        "agent_shell.tmux",
    ),
    (
        "agent_shell.models.agent",
        "import agent_shell",
        "agent_shell.interactive",
    ),
])
def test_public_facades_cannot_bypass_boundaries(source_copy, source, statement, target):
    # Arrange
    _append(source_copy, source, statement)

    # Act
    checked = _check(source_copy)

    # Assert
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout


@pytest.mark.parametrize(("module", "contract"), [
    ("agent_shell.unclassified", "package-layers"),
    ("agent_shell.adapters.new_agent_adapter", "adapter-layers"),
    ("agent_shell.adapters.new_shared_helper", "adapter-layers"),
])
def test_new_source_areas_require_classification(source_copy, module, contract):
    # Arrange
    _append(source_copy, module, '"""A new, as yet unclassified source area."""')

    # Act
    unchecked = _check(source_copy, contract, enabled=False)
    checked = _check(source_copy, contract)

    # Assert
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "BROKEN" in checked.stdout
    assert module in checked.stdout


@pytest.mark.parametrize("agent", [
    "claude_code", "codex", "copilot_cli", "cursor", "grok", "opencode", "pi",
])
def test_every_concrete_agent_is_protected(source_copy, agent):
    # Arrange
    source = "agent_shell.interactive"
    target = f"agent_shell.adapters.{agent}_adapter"
    _append(source_copy, source, f"import {target}")

    # Act
    checked = _check(source_copy, "concrete-adapters")

    # Assert
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "Only composition imports concrete agent adapters BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout


@pytest.mark.parametrize("helper", [
    "health", "response", "outcome", "process_failure", "stderr_format", "tool_denial",
])
@pytest.mark.parametrize("reverse", [False, True], ids=["session-to-helper", "helper-to-session"])
def test_shared_session_and_adapter_helpers_can_reuse_each_other(source_copy, helper, reverse):
    # Arrange: roles, not the adapters/ directory name, determine allowed dependencies.
    source = "agent_shell.interactive"
    target = f"agent_shell.adapters.{helper}"
    if reverse:
        source, target = target, source
    _append(source_copy, source, f"import {target}")

    # Act
    checked = _check(source_copy)

    # Assert
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "0 broken" in checked.stdout


@pytest.mark.parametrize("source", ["agent_shell.resources", "agent_shell.adapters.codex_adapter"])
def test_pi_resource_helper_is_owned_by_pi_adapter(source_copy, source):
    # Arrange
    target = "agent_shell.adapters.pi_resources"
    _append(source_copy, source, f"import {target}")

    # Act
    unchecked = _check(source_copy, "pi-resource-helper", enabled=False)
    checked = _check(source_copy, "pi-resource-helper")

    # Assert
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "Only the Pi adapter imports Pi resource preparation BROKEN" in checked.stdout
    assert source in checked.stdout
    assert target in checked.stdout
