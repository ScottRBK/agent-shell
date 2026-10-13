"""Keep concrete agent-adapter imports in the composition module, not shared code.

Check static imports, including local and TYPE_CHECKING imports. This is not a check for
agent-specific behaviour, dynamic imports, or arbitrary access through re-exported names.
"""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_shell"
# Discover new adapters following the same naming convention without a hand-maintained list.
ADAPTER_MODULES = {
    f"agent_shell.adapters.{path.stem}"
    for path in (PACKAGE_ROOT / "adapters").glob("*_adapter.py")
}
CODEX_MODULE = "agent_shell.adapters.codex_adapter"


def _concrete_adapter_imports(
    source: str, package: str, adapters: set[str],
) -> list[tuple[int, str]]:
    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        for adapter in sorted(adapters):
            if any(name == adapter or name.startswith(adapter + ".") for name in names):
                violations.append((node.lineno, adapter))
    return sorted(violations)


@pytest.mark.parametrize(
    "path",
    # shell.py is the composition module: it imports concrete adapters to construct them.
    [path for path in sorted(PACKAGE_ROOT.rglob("*.py")) if path != PACKAGE_ROOT / "shell.py"],
    ids=lambda path: path.relative_to(PACKAGE_ROOT).as_posix(),
)
def test_only_composition_imports_concrete_agent_adapters(path):
    # Arrange: shared helpers under adapters/ are included, as are package re-exports.
    assert ADAPTER_MODULES, "No concrete adapters found; check adapter discovery"
    relative = path.relative_to(PACKAGE_ROOT)
    package = ".".join(("agent_shell", *relative.parts[:-1]))
    source = path.read_text(encoding="utf-8")

    # Act
    violations = _concrete_adapter_imports(source, package, ADAPTER_MODULES)

    # Assert
    assert not violations, "\n".join(
        f"{relative}:{line}: imports {module}; use a protocol or a shared helper instead"
        for line, module in violations
    )


@pytest.mark.parametrize(("source", "line"), [
    ("from agent_shell.adapters.codex_adapter import CodexAdapter", 1),
    ("from agent_shell.adapters.codex_adapter import CodexAdapter as Adapter", 1),
    ("import agent_shell.adapters.codex_adapter as codex", 1),
    ("from agent_shell.adapters import codex_adapter as codex", 1),
    ("from agent_shell.adapters.codex_adapter import *", 1),
    ("from .codex_adapter import CodexAdapter", 1),
    ("from . import codex_adapter as codex", 1),
    ("from ..adapters.codex_adapter import CodexAdapter", 1),
    ("from ..adapters import codex_adapter", 1),
    ("def load():\n    from agent_shell.adapters.codex_adapter import CodexAdapter", 2),
    ("if TYPE_CHECKING:\n    from agent_shell.adapters.codex_adapter import CodexAdapter", 2),
])
def test_import_checker_rejects_a_concrete_adapter(source, line):
    # Arrange
    adapters = {CODEX_MODULE}

    # Act
    violations = _concrete_adapter_imports(source, "agent_shell.adapters", adapters)

    # Assert
    assert violations == [(line, CODEX_MODULE)]


@pytest.mark.parametrize("source", [
    "from agent_shell.models.agent import StreamEvent",
    "from agent_shell.adapters.agent_adapter_protocol import AgentAdapter",
    "from agent_shell.adapters.outcome import failure_reason",
    "from .agent_adapter_protocol import AgentAdapter",
    "from . import outcome",
    "from ..models.agent import StreamEvent",
    "import codex_adapter",  # Not a module in this package.
    "# from agent_shell.adapters.codex_adapter import CodexAdapter",
    'example = "from agent_shell.adapters.codex_adapter import CodexAdapter"',
])
def test_import_checker_allows_contracts_helpers_and_non_import_text(source):
    # Arrange
    adapters = {CODEX_MODULE}

    # Act
    violations = _concrete_adapter_imports(source, "agent_shell.adapters", adapters)

    # Assert
    assert violations == []


def test_import_checker_reports_each_adapter_in_a_multiple_import():
    # Arrange
    claude_module = "agent_shell.adapters.claude_code_adapter"
    source = "from agent_shell.adapters import codex_adapter, claude_code_adapter"

    # Act
    violations = _concrete_adapter_imports(
        source, "agent_shell", {CODEX_MODULE, claude_module},
    )

    # Assert
    assert violations == [(1, claude_module), (1, CODEX_MODULE)]
