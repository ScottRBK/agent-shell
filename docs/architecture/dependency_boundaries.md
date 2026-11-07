# Dependency boundaries

AgentShell uses ports and adapters, but its folders are not a strict domain/application/adapters
layout. The rules follow responsibilities, including shared helpers inside `adapters/`.
The contracts live in `pyproject.toml` and are checked by Import Linter
(development dependency only).

## Local check

```bash
uv sync --locked --group dev
uv run --no-sync lint-imports --no-cache
uv run --no-sync pytest tests/unit tests/integration -v
```

CI and release builds must run both the import check and functional tests. No service credentials,
agent CLIs, or model calls are needed for the import check or its regression tests.

## Permitted dependencies

From outside to inside:

1. **Composition:** `shell.py` selects concrete agents and default execution/isolation policies.
   It is deliberately not a pure application module.
2. **Integrations:** concrete agent adapters, execution hosts, workers, process/terminal utilities,
   and local model discovery. These can use application behaviour, ports, and models.
   Execution helpers may collaborate; one concrete agent adapter cannot depend on another.
3. **Shared behaviour:** interactive session delivery, resource selection/lifetime, response
   aggregation, health/outcome logic, process-failure formatting, stderr formatting, and canonical
   tool-name translation. These must
   not depend on concrete integrations, even through another helper or a composition facade.
4. **Ports:** `ports/agent.py`, `ports/execution.py`, `ports/interactive.py`, and `ports/resources.py`.
   These define contracts and transfer objects, and may depend on models and each other.
5. **Models:** `models/` depends on no other AgentShell source area.

Top-level source modules and the contents of `adapters/` are classified exhaustively. Adding a new
module there requires choosing its role in the configuration, rather than silently escaping checks.
Submodules inherit their classified package's boundaries. Generated `_version.py` is excluded from
classification only; it is not a general exception to dependency rules.

The [architecture diagram](class_diagram.md) describes the ports and their implementations.
The [dependency audit](../development/import_linter_audit.md) records the development tool's
provenance and advisory assessment.

The package-level layer groups `interactive` with `adapters/` and infrastructure because that
folder contains both concrete integrations and shared helpers. The other contracts refine this:
shared behaviour may reuse shared behaviour in either location, while `shared-no-infrastructure`
forbids its direct and indirect dependencies on execution/discovery infrastructure and composition.
Concrete agent imports remain restricted to `shell.py`. All contracts run together in CI.
The Pi resource helper has an additional protected-module
contract permitting only `pi_adapter.py` to import it. Its native installer is infrastructure,
while the shared `resources.py` workflow remains forbidden from importing infrastructure.

## Intentional details

- Only `shell.py` may directly import concrete agent adapter modules.
- `adapters/model_discovery.py` is local execution infrastructure, not pure application logic.
  Model discovery and MCP operations remain local even when execution uses a different host.
- Direct adapter construction retains native-host defaults. Hosts, isolation implementations,
  process guardians, workers, and transport framing are infrastructure, not domain models.
- `PreparedLaunch` remains responsible for its existing descriptor lifecycle. Moving its contract
  does not change ownership or make it a pure value object.
- `execution.py`, `interactive.py`, and `adapters/agent_adapter_protocol.py` retain old port import
  paths. New inner code imports ports directly. Public package exports remain supported for callers;
  inner code must not use a facade to reach an otherwise forbidden implementation.

## How the rules are tested

`tests/integration/test_architecture.py` runs the real CLI against disposable source copies.
For each contract it demonstrates that an absent rule permits a deliberate violation, then checks
that the enabled rule rejects it and identifies the offending dependency. Valid imports and the
actual repository must pass too. Graph caching is disabled so changed fixtures cannot use old data.

Tests cover direct and indirect paths where the contract promises them, conditional/local imports,
re-exports, legitimate composition, and newly unclassified modules. The old handwritten AST parser
is not retained as a second implementation of import analysis.

## Limits

These are static dependency checks, not a sandbox or a proof that every implementation is clean.
They do not detect agent-specific logic copied into shared code without importing an adapter,
computed dynamic imports, source strings executed as programs, or arbitrary runtime file/network
operations. Python's implicit package initialization and bare script-local worker imports are not
full runtime execution graphs. Imports inside `TYPE_CHECKING` and functions remain checked.

Keep the behavioural guidance in AGENTS.md and review provider-specific changes. Do not weaken a
contract merely to make a feature pass: discuss a boundary change or move the implementation to
its correct owner.
