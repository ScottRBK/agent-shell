# Package Management

`AgentShell` and `AgentAdapter` expose `add_package(PackageSpec, timeout=120.0)`, `list_packages()`,
and `remove_package(source, timeout=120.0)`. `PackageSpec` is a frozen model with a single `source`
string. Pi implements the lifecycle; other adapters raise `NotImplementedError`.

Pi uses its native install/remove commands with user scope and reads configured packages directly
from `settings.json`. Configuration follows `PI_CODING_AGENT_DIR`, falling back to `~/.pi/agent`.
No new runtime directory or evaluation isolation is introduced: callers own container mappings and
per-run isolation. Like MCP management, these operations run locally and inherit the environment,
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
