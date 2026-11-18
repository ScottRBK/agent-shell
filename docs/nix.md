# Nix

This repository is built with Nix (flake-parts + uv2nix). The Python
development loop (`uv`) keeps working unchanged; Nix adds a reproducible
package, a locked dev shell, and checks.

## Prerequisites

Nix with flakes enabled. Optionally `nix-direnv` / `direnv` for an
auto-activated shell (`.envrc` is not committed).

## Development

```bash
nix develop          # locked env from uv.lock: python 3.12, pytest, uv, ruff
pytest tests/unit tests/integration
```

The dev shell owns the environment; `uv` is present for lockfile
operations only (`uv add`, `uv lock`). `UV_NO_SYNC=1` stops `uv` from
syncing packages into a venv, but does not prevent venv creation. `uv run`
resolves its own interpreter and can create a dependency-free `.venv` with a
different Python; run `pytest` directly in the Nix shell.

`tests/e2e` is deliberately excluded from Nix builds and checks: those
tests invoke real agent CLIs and incur API costs. Run them locally with
`uv run pytest -m e2e` as before.

## Build

```bash
nix build .#agent-shell-py    # the library, installed into a python3.12 site-packages
nix build .#env               # a python interpreter with the library already inside it
```

`#env` is what to install when something must actually run python against the
library. `python3.withPackages` will not pick up `#agent-shell-py`: uv2nix
packages carry no `pythonModule` marker, so an environment built that way
imports nothing. Use `#env`, or put `#agent-shell-py` on `PYTHONPATH` when you
are composing your own environment.

## Versioning

Pure builds always report `0.0.0+dev` and never fail on missing git
metadata. Release builds stamp the real tag (run on a clean checkout of
the tag):

```bash
AGENT_SHELL_VERSION=$(git describe --tags --abbrev=0) nix build --impure
```

`--abbrev=0` matters: raw `git describe --tags` output
(`v0.4.0-3-g42dd27b`) is not PEP 440 and hatch-vcs rejects it; the bare
tag normalizes cleanly (`v0.4.0` -> `0.4.0`).

## Checks

```bash
nix flake check
```

- `pre-commit` — provided by git-hooks.nix; runs the default-stage hooks
  (`nixfmt`, `deadnix`, `statix`).
- `zero-runtime-deps` — asserts the installed distribution declares no
  `Requires-Dist` entries and imports cleanly. The package is stdlib-only
  by contract.
- `built-import` — imports the packaged module tree with no source directory on
  `sys.path`, and checks that every module in `src/` also exists in the build. The
  suite below runs against the sources, so this is what catches a module the build
  omits or a package that does not import as installed.
- `tests` — the unit and integration suites in the Nix sandbox: no network,
  no credentials, no `e2e` marker. `tests/e2e` is excluded by design because
  those tests invoke real agent CLIs and incur API cost. Run them locally with
  `uv run pytest -m e2e` as before. Tests of real PID-namespace behavior may
  also skip when the check environment cannot create the namespace.

The Python and spell hooks (`ruff`, `typos`) are wired but pinned to the
`manual` stage, because the repository has no lint configuration yet and a
default-stage hook would fail `nix flake check` on a clean checkout. Run them
on demand:

```bash
pre-commit run ruff --hook-stage manual --all-files
pre-commit run typos --hook-stage manual --all-files
```

## Non-flake usage

`default.nix` and `shell.nix` are provided via
[flake-compat](https://github.com/NixOS/flake-compat), so the project also
works under stable Nix without the `nix-command` and `flakes`
experimental-features, and under tooling with no native flake support:

```bash
nix-build .      # builds the package (same store path as `nix build .#agent-shell-py`)
nix-shell .      # enters the dev shell above
```

## Practice decisions

Each row below is a decision with the reason beside it, so this table is the
whole record - there is nothing else to consult. The rule applied throughout is
that a non-obvious choice needs a one-line justification, and so does a
departure from what the surrounding project already does.

| Decision | Choice | Why |
|------|--------|-----|
| Channel | `nixos-unstable` | zero runtime deps; instability ages only the build-time toolchain, never a consumer's runtime |
| Release | tags | existing `v*` tag history; consumers pin tags |
| Follows | `git-hooks.nix`, `flake-parts -> nixpkgs-lib`, pyproject-nix trio -> shared `nixpkgs` | single nixpkgs in the closure; shared tooling substitutes |
| Hooks | git-hooks.nix: `nixfmt`, `deadnix`, `statix` at the default stage; `ruff` and `typos` wired but `manual` | the repo has no lint config yet, so auto-running Python linters would fail a clean-checkout `nix flake check` and impose enforcement upstream never opted into; the Nix hooks are safe because they have no project configuration to get wrong |
| Version identity | `scm-stamp` — `SETUPTOOLS_SCM_PRETEND_VERSION` from `AGENT_SHELL_VERSION`, default `0.0.0+dev` | the flake source carries no `.git`; pure builds must not fail. Release recipe needs `git describe --tags --abbrev=0` — raw describe output is not PEP 440 |
| Test tiers | unit + integration as a sandboxed check; `e2e` never in a check | e2e needs real CLIs, credentials and API spend; the sandbox has no network, so anything needing it fails closed |
| Architectures | `x86_64-linux`, `aarch64-linux`, `aarch64-darwin` | `x86_64-darwin` omitted as deprecated upstream. No aarch64 builder exists today, so `nix flake check` reports those two as omitted systems — listing them is intent, not verification |
| Non-flake entry point | `flake-compat` (`default.nix` + `shell.nix`) | costs a flake-native user nothing, and the entry points delegate rather than duplicate |
| Source filtering | allow-list `lib.fileset.toSource` (`pyproject.toml`, `README.md`, `LICENSE`, `src/`) | tree is ours; docs-only commits leave the package drv hash stable |
| Binary cache | none yet — planned as a separate follow-up | intended shape is a public cache published from the default branch only, with a pull-only PR gate, SHA-pinned actions and a loud failure if the token is missing; not wired because the cache itself does not exist yet |
| CI | existing GitHub workflows plus a `nix flake check` job | the Nix job runs on pull requests and pushes to the default branch, so a `flake.lock` bump is validated before merge rather than after. It runs the full check rather than an evaluation-only pass, because proving the flake parses says nothing about whether the tests still pass |

`flake.lock` node count: **8**, with a single `nixpkgs` node — every input
that can follow our nixpkgs does, including `flake-parts -> nixpkgs-lib` and
`pyproject-nix`. (`git-hooks.nix` follows our `flake-compat` edge for the same
reason: one node per input, not one per consumer.)
