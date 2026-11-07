# Import Linter development-dependency audit

Audit date: 2026-10-14. Scope: base Import Linter CLI for local/CI architecture checks, no UI extra.
No new runtime dependency for AgentShell. Audit performed before dependency addition/installation.

## Versions and provenance

Checked official PyPI metadata, wheel contents and RECORD hashes, upstream repositories/releases,
source archives, and exact-version OSV queries. The six new Python packages are:

- `import-linter 2.15`: BSD-2-Clause, https://github.com/seddonym/import-linter
- `grimp 3.17`: BSD-2-Clause, https://github.com/seddonym/grimp
- `click 8.5.0`: BSD-3-Clause, https://github.com/pallets/click
- `rich 15.0.0`: MIT, https://github.com/Textualize/rich
- `markdown-it-py 4.2.0`: MIT, https://github.com/executablebooks/markdown-it-py
- `mdurl 0.1.2`: MIT, https://github.com/executablebooks/mdurl

Existing Pygments 2.20.0 and typing-extensions 4.15.0 satisfy shared dependencies;
no upgrade needed.
PyPI/OSV reported no Python-package advisories for these exact versions at audit time. This did NOT
establish an advisory-free native dependency graph (see below). Wheel hashes matched PyPI and
internal RECORD hashes; no startup `.pth`/customize hooks were found. Python 3.12 is supported.

Import Linter, Grimp, Rich, Markdown-it-py and mdurl had no PyPI attestation endpoint available.
Click and typing-extensions had provenance responses. Absence of attestations is a residual risk;
matching hashes alone does not prove publisher trust. Grimp contains a compiled Rust extension.
UI extra dependencies are not installed; the UI is not started. Browser assets are bundled in the
base wheel. Grimp embeds Ruff parser crates; this does not add a separate Ruff Python dependency
or a new linting job.

## Native findings and disposition

The independent audit initially recommended blocking until a reachability assessment or patched
wheel was available. Follow-up source inspection produced this bounded assessment:

- `RUSTSEC-2026-0204`: Grimp 3.17's source lock and inspected Linux/Windows wheels reference
  crossbeam-epoch 0.9.18. The advisory affects pointer formatting for `Atomic`/`Shared`; 0.9.20
  fixes it. Dependency path: Grimp -> Rayon -> rayon-core -> crossbeam-deque -> crossbeam-epoch.
  Downloaded the four relevant crate sources and verified Cargo.lock checksums. Grimp's Rust
  sources contain no crossbeam/pointer-formatting calls. Crossbeam-deque's pointer storage is
  private; its Worker/Stealer/Injector Debug implementations print constant labels, not pointers.
  Searches of the epoch/deque/Rayon sources found no pointer-formatting invocation outside the
  affected implementations themselves. The advisory explicitly excludes normal Debug formatting.
  No reachable use was identified for the CLI's import-graph operations. This is source inspection,
  not a formal reachability proof or reproducible verification of the distributed binary.
- `RUSTSEC-2024-0449`: unsafe-libyaml 0.2.11 is unmaintained, via serde_yaml in Grimp. This is a
  maintenance advisory, not a demonstrated vulnerability in this use. Treat as residual upstream
  maintenance risk and revisit when upgrading Grimp.

Decision: allow the pinned, development-only CLI for repository checks following this assessment;
retain the findings rather than claiming a clean audit. Reassess before any upgrade or enabling
additional features. No source build, replacement native binary, or blanket advisory suppression.
The independent auditor reviewed this follow-up assessment and withdrew the installation block,
confirming the crate checksums/source matches and that the installed Linux extension matches the
audited wheel. This does not remove the upstream component advisory.

## Inspected wheel SHA256

- Import Linter 2.15, universal:
  `9aaf16a88ac1e99d5a464cd7f66b6a05f7060bfa761162e0ed441773a267ed3b`
- Grimp 3.17, CPython 3.12 Linux x86_64:
  `ecf26d99a35ab7b5966af56118bb9344d1fd1b972d3c45dd5012effc7a68c844`
- Grimp 3.17, CPython 3.12 Windows x86_64:
  `d10ae67a1ee0af6dee937e032f3acf707741d866a67f3c71e5ec6df3b758e5c0`
- Click 8.5.0:
  `255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360`
- Rich 15.0.0:
  `33bd4ef74232fb73fe9279a257718407f169c09b78a87ad3d296f548e27de0bb`
- Markdown-it-py 4.2.0:
  `9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a`
- mdurl 0.1.2:
  `84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8`

`uv.lock` records resolver-selected artifacts for supported platforms; inspection above is limited
to the listed wheels, not every platform wheel. Existing dependency versions must remain unchanged.

References: https://rustsec.org/advisories/RUSTSEC-2026-0204.html and
https://rustsec.org/advisories/RUSTSEC-2024-0449.html. Local raw evidence was collected under
`/tmp/import-linter-audit-_k_yy1vb`; it is temporary and not required to run the checks.
