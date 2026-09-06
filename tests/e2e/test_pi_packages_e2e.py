"""Real Pi package lifecycle using local sources only: no downloads or model requests."""
import asyncio
import json
import shutil

import pytest

from agent_shell.models.agent import AgentType, PackageSpec
from agent_shell.shell import AgentShell


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not shutil.which("pi"), reason="Pi CLI required")]


@pytest.fixture
def pi_workspace(tmp_path, monkeypatch):
    config = tmp_path / "agent"
    config.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("PI_OFFLINE", "1")
    monkeypatch.setenv("PI_TELEMETRY", "0")
    return tmp_path


@pytest.mark.parametrize("directory_package", [False, True])
async def test_local_package_lifecycle_survives_fresh_shells(pi_workspace, directory_package):
    # Arrange
    source = pi_workspace / "local-package"
    if directory_package:
        (source / "extensions").mkdir(parents=True)
        extension = source / "extensions" / "test.js"
    else:
        source = source.with_suffix(".js")
        extension = source
    extension.write_text("export default function (pi) {}")
    spec = PackageSpec(str(source))
    shell = AgentShell(AgentType.PI)

    # Act
    await shell.add_package(spec)
    await shell.add_package(spec)
    installed = await AgentShell(AgentType.PI).list_packages()
    await AgentShell(AgentType.PI).remove_package(installed[0].source)
    remaining = await AgentShell(AgentType.PI).list_packages()

    # Assert
    assert installed == [spec]
    assert remaining == []
    assert extension.is_file()  # Removing a local registration must preserve the caller's files.


async def test_symlinked_extension_keeps_its_registered_path(pi_workspace):
    # Arrange — Pi identifies local packages by the link path, not its current target.
    target = pi_workspace / "target.js"
    target.write_text("export default function (pi) {}")
    extension = pi_workspace / "linked.js"
    extension.symlink_to(target)
    spec = PackageSpec(str(extension))
    shell = AgentShell(AgentType.PI)

    # Act
    await shell.add_package(spec)
    installed = await AgentShell(AgentType.PI).list_packages()
    await shell.remove_package(spec.source)
    remaining = await shell.list_packages()

    # Assert
    assert installed == [spec]
    assert remaining == []
    assert extension.is_symlink()
    assert target.is_file()


async def test_fresh_shell_loads_registered_extension_on_stream(pi_workspace):
    # Arrange — handle input in the extension so this test never sends a model request.
    marker = pi_workspace / "extension-ran.txt"
    extension = pi_workspace / "capture.js"
    extension.write_text(
        'import { writeFileSync } from "node:fs";\n'
        'export default function (pi) {\n'
        '  pi.on("input", (event) => {\n'
        f'    writeFileSync({json.dumps(str(marker))}, event.text);\n'
        '    return { action: "handled" };\n'
        '  });\n'
        '}\n'
    )
    await AgentShell(AgentType.PI).add_package(PackageSpec(str(extension)))

    # Act
    async with asyncio.timeout(30):
        events = [event async for event in AgentShell(AgentType.PI).stream(
            cwd=str(pi_workspace), prompt="package lifecycle smoke test",
        )]

    # Assert — the file is the extension's observable output, not adapter internals.
    assert marker.read_text() == "package lifecycle smoke test"
    assert not [event for event in events if event.type == "error"]


async def test_readonly_settings_do_not_report_successful_install(pi_workspace):
    # Arrange — Pi can exit zero even when persisting settings fails.
    settings = pi_workspace / "agent" / "settings.json"
    settings.write_text("{}")
    settings.chmod(0o400)
    extension = pi_workspace / "readonly-test.js"
    extension.write_text("export default function (pi) {}")

    # Act / Assert
    try:
        with pytest.raises(RuntimeError, match="persist|settings"):
            await AgentShell(AgentType.PI).add_package(PackageSpec(str(extension)))
    finally:
        settings.chmod(0o600)
