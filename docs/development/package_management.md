# Package Management

`AgentShell` and `AgentAdapter` expose `add_package(PackageSpec, timeout=120.0)`, `list_packages()`,
and `remove_package(source, timeout=120.0)`. `PackageSpec` is a frozen model with a single `source`
string. Pi implements the lifecycle; other adapters raise `NotImplementedError`.

Pi uses its native install/remove commands with user scope and reads configured packages directly
from `settings.json`. Configuration follows `PI_CODING_AGENT_DIR`, falling back to `~/.pi/agent`.
These explicit user-scope operations introduce no new runtime directory: callers own container
mappings and per-run isolation. Like MCP management, these operations run locally and inherit the environment,
independently of the selected execution host/isolation policy.

Installs accept exact npm versions, Git sources with an explicit ref, and existing local files or
package directories. Relative input paths resolve against the Python process cwd. Listing returns
configured package sources with local paths normalized to absolute paths. It does not prove that
resources loaded.
Standalone `extensions` entries and project-local packages are outside this initial API.

Pi can exit zero even when saving settings fails. Package operations reject malformed settings
before modification and verify persistence after the command exits. Timeouts and cancellation clean
up the command's process group. Local-only Pi E2Es verify repeat registration, removal, loading by a
fresh shell, and read-only settings failure without downloads or model requests.

See [usage examples](../examples.md#packages-and-local-extensions).

## Per-session package preparation

`SessionResources(packages=[PackageSpec(...)])` selects packages for one Pi session. It is separate
from `add_package`: the latter intentionally changes personal defaults. Session preparation accepts
existing local files/directories, exact npm versions, and Git sources with an explicit `@ref`.
Local session paths resolve against the session `cwd`, retain symlink identities, and load in place.
Prefer Git commit IDs to mutable tags or branches when repeatability matters.

A missing remote selection is installed automatically using native `pi install` in a dedicated
AgentShell cache, then passed to the runtime as a local `--extension` path. Preparation runs
locally,
independently of the selected execution host and isolation policy. Runtime credential locations,
`HOME`, and `PI_CODING_AGENT_DIR` remain unchanged. Only the package-management subprocess receives
an isolated Pi directory; no native login credentials are copied or linked into it.

The cache is `$XDG_CACHE_HOME/agent-shell/pi-packages`, defaulting to
`~/.cache/agent-shell/pi-packages`. Each source/version has its own directory and POSIX file lock.
Concurrent processes requesting the same source wait for a complete install and reuse it; different
sources have independent locks. Reuse concerns this cache, not unrelated personal/global installs.
All source specifications and unsupported resource options are validated before the first install.

Preparation allows 120 seconds per package, including lock waiting. Native failures, incomplete
installation/list output, and timeouts fail the launch before model work. Cancellation propagates
and cleans up the package command. Failed attempts may leave downloads, but no ready marker is
written; a later attempt retries. Pi's installed-path listing is checked before accepting
an install.
The current installer requires POSIX locks (Linux, macOS, WSL). Local resources do not need them.

Personal/project package registrations are unchanged. AgentShell-owned cache registrations and
completed downloads persist for reuse; they can be removed when no sessions use them. Ordinary
runtime logs, credential refresh, history and other native state remain harness-owned. Selected
package code can execute during loading. Native settings may still resolve unrelated packages;
Pi resource exclusion adds `--offline` to prevent their startup downloads after selected packages
have been prepared. Without exclusion, normal Pi startup discovery remains active.

CI substitutes only the external CLI for installation, failure and concurrency checks. Real Pi
E2Es install a tiny package from a local npm registry, load it without inference, reuse the cache,
and check unchanged personal settings. They do not verify public-registry credentials, remote Git
hosts, or model authentication. Other harnesses reject session `packages` requests.
