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
operations only (`uv add`, `uv lock`). `UV_NO_SYNC=1` is set so `uv`
never provisions a competing venv.

`tests/e2e` is deliberately excluded from Nix builds and checks: those
tests invoke real agent CLIs and incur API costs. Run them locally with
`uv run pytest -m e2e` as before.

## Build

```bash
nix build .#agent-shell-py    # the library, installed into a python3.12 site-packages
nix build                      # same, via packages.default
```

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

- `zero-runtime-deps` — asserts the installed distribution declares no
  `Requires-Dist` entries and imports cleanly. The package is stdlib-only
  by contract.

## Practice matrix decisions

Decisions follow the house Nix practice matrix (2026-11-06); deviations
carry a one-line reason.

| Axis | Choice | Why |
|------|--------|-----|
| Channel | `nixos-unstable` | zero runtime deps; instability ages only the build-time toolchain, never a 26.05 estate runtime |
| Release | tags | existing `v*` tag history; consumers pin tags |
| Follows | `git-hooks.nix`, `flake-parts -> nixpkgs-lib`, pyproject-nix trio -> shared `nixpkgs` | single nixpkgs in the closure; shared tooling substitutes |
| Hooks | git-hooks.nix: nixfmt, deadnix, statix, ruff (lint + format), typos | mirrors the Forgetful hook set |
| Source filtering | allow-list `lib.fileset.toSource` (`pyproject.toml`, `README.md`, `LICENSE`, `src/`) | tree is ours; docs-only commits leave the package drv hash stable |
| Binary cache | public Cachix (follow-up PR) | publish on `main` only, pull-only on PRs, SHA-pinned actions, loud missing-token check |
| CI | unchanged GitHub workflows | Nix jobs arrive with the Cachix follow-up |

`flake.lock` node count: TBD (recorded after final lock).
