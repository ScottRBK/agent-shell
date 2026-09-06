import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_shell.models.agent import AgentType, PackageSpec
from agent_shell.shell import AgentShell


@pytest.fixture
def pi_config(tmp_path, monkeypatch):
    directory = tmp_path / "pi-agent"
    directory.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(directory))
    monkeypatch.chdir(tmp_path)
    return directory


def _successful_process(config, sources):
    """Model Pi's external effect: persist the sources before the command exits zero."""
    process = AsyncMock(returncode=0)

    async def communicate(*args):
        (config / "settings.json").write_text(json.dumps({"packages": sources}))
        return b"Package command completed\n", b""

    process.communicate.side_effect = communicate
    return process


async def test_fresh_shell_lists_configured_packages(pi_config):
    # Arrange — Pi supports strings and objects with resource filters in user settings.
    (pi_config / "settings.json").write_text(json.dumps({
        "packages": [
            "npm:@example/tools@1.2.3",
            {"source": "git:github.com/example/tools@v2", "skills": []},
            "../my-extension.ts",
        ],
        "extensions": ["/unmanaged/extension.ts"],
    }))

    # Act
    packages = await AgentShell(AgentType.PI).list_packages()

    # Assert — local sources are usable from any subsequent working directory.
    assert packages == [
        PackageSpec(source="npm:@example/tools@1.2.3"),
        PackageSpec(source="git:github.com/example/tools@v2"),
        PackageSpec(source=str(pi_config.parent / "my-extension.ts")),
    ]


async def test_missing_settings_means_no_configured_packages(pi_config):
    # Arrange
    shell = AgentShell(AgentType.PI)

    # Act
    packages = await shell.list_packages()

    # Assert
    assert packages == []


async def test_symlinked_config_preserves_pi_local_package_identity(pi_config, monkeypatch):
    # Arrange — Pi resolves relative sources lexically, without following directory symlinks.
    (pi_config / "settings.json").write_text('{"packages": ["../tool.js"]}')
    mapped = pi_config.parent / "mapped"
    mapped.mkdir()
    config_link = mapped / "agent"
    config_link.symlink_to(pi_config, target_is_directory=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_link))

    # Act
    packages = await AgentShell(AgentType.PI).list_packages()

    # Assert — this is the path Pi will use, not the target directory's sibling.
    assert packages == [PackageSpec(str(mapped / "tool.js"))]


@pytest.mark.parametrize("source", [
    "npm:@example/tools@1.2.3",
    "git:github.com/example/tools@v2",
])
async def test_installs_pinned_package_through_pi_cli(pi_config, source):
    # Arrange
    process = _successful_process(pi_config, [source])
    shell = AgentShell(AgentType.PI)

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process) as launch:
        await shell.add_package(PackageSpec(source=source))

    # Assert — argv and inherited environment are the external CLI contract.
    assert launch.call_args.args == ("pi", "install", source, "--no-approve")
    assert launch.call_args.kwargs["cwd"] == str(pi_config.parent)
    assert launch.call_args.kwargs["env"] is None


@pytest.mark.parametrize("symlink", [False, True])
async def test_registers_local_extension_with_literal_absolute_path(pi_config, symlink):
    # Arrange — spaces and shell syntax are valid filename characters, not executable code.
    extension = pi_config.parent / "tools $(touch unwanted).ts"
    if symlink:
        target = pi_config.parent / "target.ts"
        target.write_text("export default function (pi) {}")
        extension.symlink_to(target)
    else:
        extension.write_text("export default function (pi) {}")
    process = _successful_process(pi_config, [str(extension)])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process) as launch:
        await AgentShell(AgentType.PI).add_package(PackageSpec(source=f"./{extension.name}"))

    # Assert
    assert launch.call_args.args == ("pi", "install", str(extension), "--no-approve")
    assert not (pi_config.parent / "unwanted").exists()


@pytest.mark.parametrize("source", ["npm:@example/tools", "git:github.com/example/tools"])
async def test_removes_package_by_unversioned_identity(pi_config, source):
    # Arrange
    (pi_config / "settings.json").write_text(json.dumps({"packages": [source]}))
    process = _successful_process(pi_config, [])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process) as launch:
        await AgentShell(AgentType.PI).remove_package(source)

    # Assert
    assert launch.call_args.args == ("pi", "remove", source, "--no-approve")


@pytest.mark.parametrize("operation", ["add_package", "remove_package"])
async def test_package_failure_reports_cli_diagnostic(pi_config, operation):
    # Arrange
    process = AsyncMock(returncode=1)
    process.communicate.return_value = (b"", b"Registry unavailable")
    argument = PackageSpec("npm:tools@1.2.3") if operation == "add_package" else "npm:tools"

    # Act / Assert
    with patch("asyncio.create_subprocess_exec", return_value=process):
        with pytest.raises(RuntimeError, match="Registry unavailable"):
            await getattr(AgentShell(AgentType.PI), operation)(argument)


async def test_install_timeout_is_clear_and_reaps_process(pi_config):
    # Arrange
    process = AsyncMock(returncode=None)

    async def hang(*args):
        await asyncio.Event().wait()

    process.communicate.side_effect = hang

    # Act / Assert
    with patch("asyncio.create_subprocess_exec", return_value=process):
        with pytest.raises(RuntimeError, match="pi install.*timed out"):
            await AgentShell(AgentType.PI).add_package(PackageSpec("npm:tools@1.2.3"), timeout=.01)
    process.wait.assert_awaited()


async def test_missing_pi_has_clear_error(pi_config):
    # Arrange
    shell = AgentShell(AgentType.PI)

    # Act / Assert
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("pi")):
        with pytest.raises(RuntimeError, match="Could not start.*pi install"):
            await shell.add_package(PackageSpec("npm:tools@1.2.3"))


@pytest.mark.parametrize("source", [
    "npm:tools", "npm:tools@latest", "npm:tools@^1.2.3", "npm:tools@1",
    "git:github.com/example/tools", "git:git@github.com:example/tools",
    "ssh://git@github.com/example/tools", "https://github.com/example/tools",
])
async def test_install_requires_explicit_remote_version(pi_config, source):
    # Arrange
    shell = AgentShell(AgentType.PI)

    # Act / Assert
    with patch("asyncio.create_subprocess_exec") as launch:
        with pytest.raises(ValueError, match="pin|version|ref"):
            await shell.add_package(PackageSpec(source))
    launch.assert_not_called()


@pytest.mark.parametrize("source", ["--help", "pip:tools", "ftp://example.com/tools", "git:"])
async def test_unsupported_sources_are_rejected_before_launch(pi_config, source):
    # Arrange
    shell = AgentShell(AgentType.PI)

    # Act / Assert
    with patch("asyncio.create_subprocess_exec") as launch:
        with pytest.raises(ValueError, match="source"):
            await shell.add_package(PackageSpec(source))
    launch.assert_not_called()


@pytest.mark.parametrize("source", [
    "npm:tools@2.0.0-beta.1",
    "git:git@github.com:example/tools@v1",
    "ssh://git@github.com/example/tools@abc123",
    "https://github.com/example/tools@v1",
])
async def test_supported_remote_sources_are_passed_unchanged(pi_config, source):
    # Arrange
    process = _successful_process(pi_config, [source])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process) as launch:
        await AgentShell(AgentType.PI).add_package(PackageSpec(source))

    # Assert
    assert launch.call_args.args[2] == source


async def test_missing_local_extension_is_rejected(pi_config):
    # Arrange
    shell = AgentShell(AgentType.PI)

    # Act / Assert
    with patch("asyncio.create_subprocess_exec") as launch:
        with pytest.raises(ValueError, match="does not exist"):
            await shell.add_package(PackageSpec("./missing.ts"))
    launch.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_package_timeout_is_rejected_before_launch(pi_config, timeout):
    # Arrange
    shell = AgentShell(AgentType.PI)

    # Act / Assert
    with patch("asyncio.create_subprocess_exec") as launch:
        with pytest.raises(ValueError, match="timeout"):
            await shell.add_package(PackageSpec("npm:tools@1.2.3"), timeout=timeout)
    launch.assert_not_called()


@pytest.mark.parametrize("settings", [
    "not json", "[]", '{"packages": {}}', '{"packages": [null]}',
    '{"packages": [{"skills": []}]}', '{"packages": [""]}',
])
async def test_malformed_package_settings_fail_clearly(pi_config, settings):
    # Arrange
    (pi_config / "settings.json").write_text(settings)

    # Act / Assert
    with pytest.raises(RuntimeError, match="Pi package settings"):
        await AgentShell(AgentType.PI).list_packages()


async def test_install_cancellation_propagates_and_reaps_process(pi_config):
    # Arrange
    process = AsyncMock(returncode=None)
    started = asyncio.Event()

    async def hang(*args):
        started.set()
        await asyncio.Event().wait()

    process.communicate.side_effect = hang

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        task = asyncio.create_task(
            AgentShell(AgentType.PI).add_package(PackageSpec("npm:tools@1.2.3"))
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Assert
    process.wait.assert_awaited()


@pytest.mark.parametrize("operation", ["add_package", "remove_package"])
async def test_broken_settings_are_rejected_before_modifying_packages(pi_config, operation):
    # Arrange — Pi may warn about malformed settings and continue; do not risk replacing them.
    (pi_config / "settings.json").write_text("{broken")
    argument = PackageSpec("npm:tools@1.2.3") if operation == "add_package" else "npm:tools"

    # Act / Assert
    with patch("asyncio.create_subprocess_exec") as launch:
        with pytest.raises(RuntimeError, match="Pi package settings"):
            await getattr(AgentShell(AgentType.PI), operation)(argument)
    launch.assert_not_called()


async def test_lists_default_pi_directory_when_no_override(tmp_path, monkeypatch):
    # Arrange — patch the filesystem home lookup without changing the process's HOME.
    from pathlib import Path

    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = tmp_path / ".pi" / "agent"
    config.mkdir(parents=True)
    (config / "settings.json").write_text('{"packages": ["npm:tools@1.2.3"]}')

    # Act
    packages = await AgentShell(AgentType.PI).list_packages()

    # Assert
    assert packages == [PackageSpec("npm:tools@1.2.3")]


@pytest.mark.parametrize("operation", ["add_package", "remove_package"])
async def test_zero_exit_without_persisted_change_is_failure(pi_config, operation):
    # Arrange — captured Pi behaviour: a settings write error need not change its exit code.
    initial = [] if operation == "add_package" else ["npm:tools@1.2.3"]
    (pi_config / "settings.json").write_text(json.dumps({"packages": initial}))
    argument = PackageSpec("npm:tools@1.2.3") if operation == "add_package" else "npm:tools"
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b"Package command completed\n", b"")

    # Act / Assert
    with patch("asyncio.create_subprocess_exec", return_value=process):
        with pytest.raises(RuntimeError, match="persist.*settings"):
            await getattr(AgentShell(AgentType.PI), operation)(argument)


async def test_version_replacement_is_visible_to_a_fresh_shell(pi_config):
    # Arrange — Pi replaces a configured npm source by package name.
    (pi_config / "settings.json").write_text('{"packages": ["npm:tools@1.2.3"]}')
    process = _successful_process(pi_config, ["npm:tools@2.0.0"])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        await AgentShell(AgentType.PI).add_package(PackageSpec("npm:tools@2.0.0"))
    packages = await AgentShell(AgentType.PI).list_packages()

    # Assert
    assert packages == [PackageSpec("npm:tools@2.0.0")]
