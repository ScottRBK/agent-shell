# Testing and CI/CD

Agent Shell has three test tiers. Each tier answers a different question and has a
different execution boundary.

| Tier | Purpose | Real agent CLI calls | User credentials or API cost | Runs in CI/CD |
|---|---|---:|---:|---:|
| Unit | Verify isolated functions, parsing, validation, and command construction | No | No | Yes |
| Integration | Verify the complete `AgentShell` → adapter → parser flow with controlled subprocess output and isolated configuration | No | No | Yes |
| E2E | Verify that the installed agent CLIs and their current output/configuration formats still work in practice | Yes | Possibly | **Never** |

## Unit tests

Unit tests live in `tests/unit/`. They exercise small, isolated behaviours such as:

- translating vendor events into `StreamEvent` objects;
- aggregating an `AgentResponse`;
- validating `MCPServerSpec` values;
- constructing CLI arguments;
- process cancellation and cleanup.

External processes and filesystem boundaries are mocked where relevant. These tests are
fast, deterministic, and intended to identify the smallest component responsible for a
failure.

Run them with:

```bash
uv run pytest tests/unit -v
```

## Integration tests

Integration tests live in `tests/integration/`. They exercise interactions between the
public `AgentShell` API and concrete adapters without calling a real agent service.
Subprocesses are mocked to emit representative NDJSON, so the same execution and parsing
path used in production is tested without credentials, network access, or API spend.

Configuration integration tests use a temporary `HOME` where possible. This verifies file
formats and round trips without reading or modifying the developer's real agent config.

Run them with:

```bash
uv run pytest tests/integration -v
```

## End-to-end tests

E2E tests live in `tests/e2e/` and are marked `e2e`. They invoke locally installed agent
CLIs, use the developer's available credentials and providers, and may incur real API
costs. Their purpose is to detect upstream CLI changes that mocks and captured fixtures
cannot reveal, such as renamed event fields, changed argument parsing, or different config
serialization.

E2E tests are explicit, local-only checks:

```bash
uv run pytest tests/e2e -v
```

> [!WARNING]
> E2E tests may mutate real user configuration files. MCP tests can call an agent's real
> `mcp add` and `mcp remove` commands, affecting files such as `~/.claude.json`,
> `~/.config/opencode/opencode.json`, `~/.copilot/mcp-config.json`, or Codex configuration.
> Tests use unique names and `finally` cleanup where implemented, but forced termination,
> a CLI crash, or a machine failure can prevent cleanup. The CLI may also rewrite config
> formatting even when the temporary entry is removed. Review the selected E2E test before
> running it, avoid running it concurrently with an active agent session, and back up
> important configuration first.

The `e2e` marker is descriptive; it does not automatically exclude these tests when running
an unrestricted `pytest` command. Use the tier-specific commands above unless deliberately
running the full local suite.

## CI/CD policy

CI and build workflows run only the deterministic tiers:

```bash
uv run pytest tests/unit tests/integration -v
```

- `.github/workflows/ci.yml` runs unit and integration tests for pushes and pull requests.
- `.github/workflows/build.yml` runs unit and integration tests for version tags before
  building release artifacts.
- `.github/workflows/publish.yml` builds and publishes a GitHub release to PyPI; it does not
  invoke E2E tests.

No CI/CD workflow runs `tests/e2e/`. This is intentional: hosted runners do not have the
required local agent installations and user credentials, E2E calls can cost money, results
depend on external services, and configuration-mutating tests must not operate on shared
automation environments.

## Recommended development sequence

1. Start with a failing unit or integration test for deterministic behaviour.
2. Implement the smallest change that makes that test pass.
3. Run `uv run pytest tests/unit tests/integration -v` before committing.
4. Run only the relevant E2E test when the change depends on real CLI behaviour.
5. Treat E2E failures as possible upstream compatibility changes and capture the behaviour
   in deterministic integration fixtures once understood.
