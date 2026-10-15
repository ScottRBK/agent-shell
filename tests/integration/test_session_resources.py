"""Session resource requests travel through the public API to the external CLI boundary."""

import asyncio
import json
from pathlib import Path

import pytest

from agent_shell.models.agent import AgentType, MCPServerSpec, MCPServerType
from agent_shell.models.resources import SessionResources
from agent_shell.shell import AgentShell


@pytest.fixture(autouse=True)
def resource_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class CaptureHost:
    def __init__(self, session_id=None):
        self.calls = []
        self.session_id = session_id

    async def launch(self, command, cwd, **kwargs):
        self.calls.append((command, cwd, kwargs))
        process = type("Run", (), {})()
        process.stdout = asyncio.StreamReader()
        if self.session_id:
            process.stdout.feed_data(json.dumps({
                "type": "session", "id": self.session_id,
            }).encode() + b"\n")
        process.stdout.feed_data(b'{"type":"agent_end","messages":[]}\n')
        process.stdout.feed_eof()
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_eof()
        process.returncode = 0
        process.wait = self.wait
        process.release = lambda: None
        return process

    async def wait(self):
        return 0


class ClaudeMCPHost(CaptureHost):
    def __init__(self):
        super().__init__()
        self.configs = []

    async def launch(self, command, cwd, **kwargs):
        config = Path(command[command.index("--mcp-config") + 1])
        self.configs.append(json.loads(config.read_text()))
        process = await super().launch(command, cwd, **kwargs)
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(
            b'{"type":"system","session_id":"mcp-session"}\n'
            b'{"type":"result","session_id":"mcp-session","is_error":false}\n'
        )
        process.stdout.feed_eof()
        return process


async def test_pi_role_loads_explicit_resources_without_changing_native_home(tmp_path, monkeypatch):
    # Arrange
    extension = tmp_path / "role.mjs"
    extension.write_text("export default function(pi) {}")
    skill = tmp_path / "role-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: role\ndescription: role\n---\nRole")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "personal"))
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)
    resources = SessionResources(
        skills=[str(skill)], extensions=[str(extension)], mcp_servers=[],
        inherit_personal_resources=False, inherit_project_resources=False,
        include_home_system_prompt=False,
    )

    # Act
    await shell.execute(str(tmp_path), "hello", resources=resources)

    # Assert
    command, cwd, options = host.calls[0]
    assert command[command.index("--skill") + 1] == str(skill)
    assert command[command.index("--extension") + 1] == str(extension)
    assert {"--no-extensions", "--no-skills", "--no-mcp", "--offline"} <= set(command)
    assert "--no-context-files" not in command
    assert command[command.index("--system-prompt") + 1] == ""
    assert "PI_CODING_AGENT_DIR" not in options.get("env", {})
    assert command[-1] == "hello"
    assert cwd == str(tmp_path)


@pytest.mark.parametrize("agent", list(AgentType))
async def test_unsupported_extension_rejected_before_launch(agent, tmp_path):
    # Arrange
    extension = tmp_path / "role.mjs"
    extension.write_text("export default function() {}")
    host = CaptureHost()
    shell = AgentShell(agent, execution_host=host)
    resources = SessionResources(extensions=[str(extension)], plugins=[str(tmp_path)])

    # Act / Assert: no harness supports both native resource kinds in this API.
    with pytest.raises(NotImplementedError):
        await shell.execute(str(tmp_path), "hello", resources=resources)
    assert host.calls == []


@pytest.mark.parametrize("agent", [AgentType.CLAUDE_CODE, AgentType.COPILOT_CLI, AgentType.CURSOR])
async def test_native_plugin_is_added_to_only_this_launch(agent, tmp_path):
    # Arrange
    host = CaptureHost()
    shell = AgentShell(agent, execution_host=host)
    plugin = tmp_path / "role"
    plugin.mkdir()

    # Act
    events = [event async for event in shell.stream(
        str(tmp_path), "hello", resources=SessionResources(plugins=[str(plugin)]),
    )]
    first = host.calls[0][0]
    _ = [event async for event in shell.stream(str(tmp_path), "hello")]

    # Assert
    assert first[first.index("--plugin-dir") + 1] == str(plugin)
    assert "--plugin-dir" not in host.calls[1][0]


async def test_invalid_paths_fail_before_external_processes(tmp_path):
    # Arrange
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    with pytest.raises(ValueError, match="path does not exist"):
        await shell.execute(str(tmp_path), "hello", resources=SessionResources(skills=["missing"]))
    assert host.calls == []


async def test_pi_installs_missing_package_once_for_concurrent_sessions(
    tmp_path, monkeypatch, missing_home,
):
    # Arrange: only the external package CLI is substituted; locking and file lifecycle are real.
    from agent_shell.models.agent import PackageSpec
    from threading import Event

    # Model a slow lock-file open that needs the event loop to remain responsive.
    real_open = Path.open
    loop = asyncio.get_running_loop()

    def slow_lock_open(path, *args, **kwargs):
        if path.suffix == ".lock":
            serviced = Event()
            loop.call_soon_threadsafe(serviced.set)
            if not serviced.wait(timeout=2):
                raise RuntimeError("Lock-file opening blocked the event loop")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_lock_open)

    binary = tmp_path / "pi"
    log = tmp_path / "installs"
    binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ["PI_CODING_AGENT_DIR"])
package = home / "npm/node_modules/tools"
if sys.argv[1] == "install":
    time.sleep(0.05)
    with open(os.environ["INSTALL_LOG"], "a") as log:
        log.write("install\\n")
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text('{"name":"tools","version":"1.2.3"}')
    (home / "settings.json").write_text(json.dumps({"packages": [sys.argv[2]]}))
elif sys.argv[1] == "list":
    print("User packages:\\n  npm:tools@1.2.3\\n    " + str(package))
else:
    raise AssertionError(sys.argv)
''')
    binary.chmod(0o755)
    import os
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("INSTALL_LOG", str(log))
    personal = tmp_path / "personal"
    personal.mkdir()
    settings = personal / "settings.json"
    settings.write_text('{"packages":["existing"]}')
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(personal))
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)
    resources = SessionResources(packages=[PackageSpec("npm:tools@1.2.3")])

    # Act
    await asyncio.gather(*[
        shell.execute(str(tmp_path), "hello", resources=resources) for _ in range(3)
    ])

    # Assert
    assert log.read_text().splitlines() == ["install"]
    assert settings.read_text() == '{"packages":["existing"]}'
    assert len(host.calls) == 3
    paths = [cmd[cmd.index("--extension") + 1] for cmd, _, _ in host.calls]
    assert len(set(paths)) == 1
    assert Path(paths[0]).is_dir()


async def test_pi_install_failure_prevents_agent_work(tmp_path, monkeypatch):
    # Arrange
    from agent_shell.models.agent import PackageSpec
    import os

    binary = tmp_path / "pi"
    binary.write_text('#!/bin/sh\necho "download failed" >&2\nexit 1\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    with pytest.raises(RuntimeError, match="download failed"):
        await shell.execute(str(tmp_path), "hello", resources=SessionResources(
            packages=[PackageSpec("npm:tools@1.2.3")],
        ))
    assert host.calls == []


class InteractiveHost:
    def __init__(self):
        self.calls = []

    async def launch_interactive(self, command, cwd, **kwargs):
        self.calls.append((command, kwargs))
        return Terminal()


class Terminal:
    closed = False

    async def close(self):
        self.closed = True


async def test_pi_interactive_resources_preserve_observer_and_cleanup(tmp_path):
    # Arrange
    extension = tmp_path / "role.js"
    extension.write_text("export default function() {}")
    host = InteractiveHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act
    session = await shell.open_interactive(str(tmp_path), resources=SessionResources(
        extensions=[str(extension)], inherit_personal_resources=False,
        inherit_project_resources=False, mcp_servers=[],
    ), prompt="hello")
    command = host.calls[0][0]
    extensions = [Path(command[i + 1]) for i, arg in enumerate(command) if arg == "--extension"]
    observer = next(path for path in extensions if path != extension)
    existed = observer.exists()
    await session.close()

    # Assert
    assert extension in extensions
    assert existed and not observer.exists()
    assert extension.exists()
    assert "--no-extensions" in command
    assert command[-2:] == ["--", "hello"]


@pytest.mark.parametrize("allowed_tools", [[], ["read"]])
async def test_invalid_interactive_tools_fail_before_package_installation(
    tmp_path, monkeypatch, allowed_tools,
):
    # Arrange: observe the external installer boundary without installing a real package.
    import os
    from agent_shell.models.agent import PackageSpec

    binary = tmp_path / "pi"
    binary.write_text('#!/bin/sh\necho invoked >> "$PI_INSTALL_LOG"\nexit 93\n')
    binary.chmod(0o755)
    log = tmp_path / "install.log"
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("PI_INSTALL_LOG", str(log))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    host = InteractiveHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    with pytest.raises(NotImplementedError, match="allowed_tools"):
        await shell.open_interactive(str(tmp_path), allowed_tools=allowed_tools,
                                     resources=SessionResources(
                                         packages=[PackageSpec("npm:tools@1.2.3")],
                                     ))
    assert not log.exists()
    assert not host.calls


async def test_resume_keeps_selection_across_fresh_shells(tmp_path):
    # Arrange
    host = CaptureHost("saved-session")
    resources = SessionResources(mcp_servers=[])
    await AgentShell(AgentType.PI, execution_host=host).execute(
        str(tmp_path), "first", resources=resources,
    )
    resumed = AgentShell(AgentType.PI, execution_host=host)

    # Act
    await resumed.execute(str(tmp_path), "second", session_id="saved-session", resources=resources)

    # Assert
    with pytest.raises(ValueError, match="selection"):
        await resumed.execute(str(tmp_path), "changed", session_id="saved-session",
                              resources=SessionResources())
    with pytest.raises(ValueError, match="selection"):
        await resumed.execute(str(tmp_path), "forgot resources", session_id="saved-session")
    assert len(host.calls) == 2
    stored = list((tmp_path / "state").rglob("*.sha256"))
    assert stored


async def test_resources_cannot_be_added_to_an_unknown_resume(tmp_path):
    # Arrange
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    with pytest.raises(ValueError, match="selection"):
        await shell.execute(str(tmp_path), "hello", session_id="external-session",
                            resources=SessionResources(mcp_servers=[]))
    assert host.calls == []


@pytest.mark.parametrize("directory_kind", ["symlink", "different"])
@pytest.mark.parametrize("supply_resources", [False, True])
async def test_resume_cannot_bypass_selection_by_changing_directory(
    tmp_path, directory_kind, supply_resources,
):
    # Arrange
    original = tmp_path / "original"
    original.mkdir()
    other = tmp_path / "other"
    if directory_kind == "symlink":
        other.symlink_to(original, target_is_directory=True)
    else:
        other.mkdir()
    host = CaptureHost("selected-session")
    resources = SessionResources(mcp_servers=[])
    await AgentShell(AgentType.PI, execution_host=host).execute(
        str(original), "first", resources=resources,
    )

    # Act / Assert: the session is known even when a different directory is submitted.
    with pytest.raises(ValueError, match="original resource selection"):
        await AgentShell(AgentType.PI, execution_host=host).execute(
            str(other), "resume", session_id="selected-session",
            resources=resources if supply_resources else None,
        )
    assert len(host.calls) == 1


@pytest.mark.parametrize("mode", ["execute", "stream"])
@pytest.mark.parametrize("change_transport", [False, True])
async def test_resume_allows_new_mcp_config_for_the_same_server_names(
    tmp_path, mode, change_transport,
):
    # Arrange
    host = ClaudeMCPHost()
    await AgentShell(AgentType.CLAUDE_CODE, execution_host=host).execute(
        str(tmp_path), "first", resources=SessionResources(mcp_servers=[
            MCPServerSpec("local", MCPServerType.STDIO, command="old-server", args=["old"],
                          env={"OLD_TOKEN": "old-secret"}),
            MCPServerSpec("remote", MCPServerType.HTTP, url="https://old.invalid/mcp",
                          headers={"Authorization": "Bearer old"}),
        ]),
    )
    local = "remote" if change_transport else "local"
    remote = "local" if change_transport else "remote"
    updated = SessionResources(mcp_servers=[
        MCPServerSpec(remote, MCPServerType.HTTP, url="https://new.invalid/mcp",
                      headers={"Authorization": "Bearer new", "X-Tenant": "other"}),
        MCPServerSpec(local, MCPServerType.STDIO, command="new-server", args=["new", "--flag"],
                      env={"NEW_TOKEN": "new-secret", "MODE": "other"}),
    ])
    shell = AgentShell(AgentType.CLAUDE_CODE, execution_host=host)

    # Act: a fresh shell resumes with reordered servers and changed configuration.
    if mode == "execute":
        await shell.execute(str(tmp_path), "resume", session_id="mcp-session", resources=updated)
    else:
        _ = [event async for event in shell.stream(
            str(tmp_path), "resume", session_id="mcp-session", resources=updated,
        )]

    # Assert: the new values actually reach the harness, rather than merely bypassing validation.
    assert len(host.calls) == 2
    assert host.configs[-1] == {"mcpServers": {
        remote: {"type": "http", "url": "https://new.invalid/mcp",
                 "headers": {"Authorization": "Bearer new", "X-Tenant": "other"}},
        local: {"type": "stdio", "command": "new-server", "args": ["new", "--flag"],
                "env": {"NEW_TOKEN": "new-secret", "MODE": "other"}},
    }}


@pytest.mark.parametrize("names", [["renamed"], ["original", "added"], [], None])
async def test_resume_still_rejects_changes_to_selected_mcp_server_names(tmp_path, names):
    # Arrange
    host = ClaudeMCPHost()
    await AgentShell(AgentType.CLAUDE_CODE, execution_host=host).execute(
        str(tmp_path), "first", resources=SessionResources(mcp_servers=[
            MCPServerSpec("original", MCPServerType.HTTP, url="https://old.invalid/mcp"),
        ]),
    )
    selected = None if names is None else [
        MCPServerSpec(name, MCPServerType.HTTP, url="https://new.invalid/mcp") for name in names
    ]

    # Act / Assert: rename, add, remove and switching to inherited discovery all change selection.
    with pytest.raises(ValueError, match="original resource selection"):
        await AgentShell(AgentType.CLAUDE_CODE, execution_host=host).execute(
            str(tmp_path), "resume", session_id="mcp-session",
            resources=SessionResources(mcp_servers=selected),
        )
    assert len(host.calls) == 1


@pytest.mark.parametrize("servers", [[], ["stdio", "http"]])
async def test_claude_mcp_selection_uses_private_strict_config(tmp_path, servers):
    # Arrange
    from agent_shell.models.agent import MCPServerSpec, MCPServerType

    selected = [] if not servers else [
        MCPServerSpec("local", MCPServerType.STDIO, command="test-server", args=["serve"],
                      env={"TOKEN": "test-secret"}),
        MCPServerSpec("remote", MCPServerType.HTTP, url="https://example.invalid/mcp",
                      headers={"Authorization": "test-secret"}),
    ]
    captured = []

    class MCPHost(CaptureHost):
        async def launch(self, command, cwd, **kwargs):
            config = Path(command[command.index("--mcp-config") + 1])
            captured.append((config, json.loads(config.read_text()), config.stat().st_mode & 0o777))
            return await super().launch(command, cwd, **kwargs)

    host = MCPHost()

    # Act
    _ = [event async for event in AgentShell(AgentType.CLAUDE_CODE, execution_host=host).stream(
        str(tmp_path), "hello", resources=SessionResources(mcp_servers=selected),
    )]

    # Assert
    command = host.calls[0][0]
    path, config, mode = captured[0]
    assert "--strict-mcp-config" in command
    assert "--bare" not in command
    assert "test-secret" not in repr(command)
    assert not path.exists()
    assert mode == 0o600
    if not servers:
        assert config == {"mcpServers": {}}
    else:
        assert config["mcpServers"]["local"]["command"] == "test-server"
        assert config["mcpServers"]["remote"]["type"] == "http"


async def test_opencode_inline_resources_preserve_existing_config_and_environment(
    tmp_path, monkeypatch,
):
    # Arrange
    import os

    plugin = tmp_path / "role.mjs"
    plugin.write_text("export default async () => ({})")
    skill = tmp_path / "skills"
    skill.mkdir()
    inherited = '{"model":"vendor/model","plugin":["existing"],"skills":{"paths":["old"]}}'
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", inherited)
    host = CaptureHost()
    shell = AgentShell(AgentType.OPENCODE, execution_host=host)

    # Act
    _ = [event async for event in shell.stream(str(tmp_path), "hello", resources=SessionResources(
        plugins=[str(plugin)], skills=[str(skill)],
    ))]

    # Assert
    config = json.loads(host.calls[0][2]["env"]["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "vendor/model"
    assert config["plugin"] == ["existing", plugin.as_uri()]
    assert config["skills"]["paths"] == ["old", str(skill)]
    assert os.environ["OPENCODE_CONFIG_CONTENT"] == inherited


async def test_closing_stream_stops_child_before_removing_resource_files(tmp_path):
    # Arrange
    stopped = []

    class BlockingHost:
        async def launch(self, command, cwd, **kwargs):
            path = Path(command[command.index("--mcp-config") + 1])
            run = type("Run", (), {})()
            run.stdout = asyncio.StreamReader()
            run.stdout.feed_data(
                b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
            )
            run.stderr = asyncio.StreamReader()
            run.stderr.feed_eof()
            run.returncode = None

            async def cancel():
                stopped.append(path.exists())
                run.returncode = -9

            run.cancel = cancel
            return run

    shell = AgentShell(AgentType.CLAUDE_CODE, execution_host=BlockingHost())
    stream = shell.stream(str(tmp_path), "hello", resources=SessionResources(mcp_servers=[]))

    # Act
    event = await anext(stream)
    await stream.aclose()

    # Assert
    assert event.content == "hi"
    assert stopped == [True]


@pytest.mark.parametrize("resume_choice", ["same", "omitted", "changed"])
async def test_cancelled_execute_retains_observed_session_selection(tmp_path, resume_choice):
    # Arrange: signal when the CLI's session event has been consumed and it awaits more output.
    waiting = asyncio.Event()
    stopped = []

    class SessionReader(asyncio.StreamReader):
        async def read(self, n=-1):
            if not self._buffer:
                waiting.set()
            return await super().read(n)

    class BlockingHost(CaptureHost):
        async def launch(self, command, cwd, **kwargs):
            run = await super().launch(command, cwd, **kwargs)
            run.stdout = SessionReader()
            run.stdout.feed_data(b'{"type":"session","id":"cancelled-session"}\n')
            run.returncode = None

            async def cancel():
                stopped.append(True)
                run.returncode = -9

            run.cancel = cancel
            return run

    resources = SessionResources(mcp_servers=[])
    shell = AgentShell(AgentType.PI, execution_host=BlockingHost())
    task = asyncio.create_task(shell.execute(str(tmp_path), "hello", resources=resources))

    # Act
    try:
        await asyncio.wait_for(waiting.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Assert: a fresh shell can resume only with the selection bound before cancellation.
    assert stopped == [True]
    host = CaptureHost(session_id="cancelled-session")
    resumed = AgentShell(AgentType.PI, execution_host=host)
    choices = {
        "same": resources,
        "omitted": None,
        "changed": SessionResources(mcp_servers=[], include_instruction_files=False),
    }
    if resume_choice == "same":
        response = await resumed.execute(
            str(tmp_path), "resume", session_id="cancelled-session", resources=resources,
        )
        assert response.session_id == "cancelled-session"
        assert len(host.calls) == 1
    else:
        with pytest.raises(ValueError, match="original resource selection"):
            await resumed.execute(
                str(tmp_path), "resume", session_id="cancelled-session",
                resources=choices[resume_choice],
            )
        assert host.calls == []


async def test_pi_exclusion_requires_explicit_mcp_choice(tmp_path):
    # Arrange
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert: --no-extensions also disables Pi's built-in MCP extension.
    with pytest.raises(NotImplementedError, match="mcp_servers"):
        await shell.execute(str(tmp_path), "hello", resources=SessionResources(
            inherit_personal_resources=False, inherit_project_resources=False,
        ))
    assert not host.calls


@pytest.mark.parametrize("auto_approve", [False, True])
@pytest.mark.parametrize("relative_cwd", [False, True])
async def test_pi_home_prompt_exclusion_preserves_trusted_project_prompt(
    tmp_path, monkeypatch, auto_approve, relative_cwd,
):
    # Arrange
    project_prompt = tmp_path / ".pi/SYSTEM.md"
    project_prompt.parent.mkdir()
    project_prompt.write_text("project override")
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act
    monkeypatch.chdir(tmp_path.parent)
    cwd = tmp_path.name if relative_cwd else str(tmp_path)
    await shell.execute(cwd, "hello", auto_approve=auto_approve,
                        resources=SessionResources(include_home_system_prompt=False))

    # Assert
    command = host.calls[0][0]
    assert command[command.index("--system-prompt") + 1] == (
        str(project_prompt) if auto_approve else ""
    )


@pytest.mark.parametrize("agent, flag, value", [
    (AgentType.COPILOT_CLI, "--no-custom-instructions", None),
])
async def test_native_instruction_exclusion(agent, flag, value, tmp_path):
    # Arrange
    host = CaptureHost()
    shell = AgentShell(agent, execution_host=host)

    # Act
    _ = [event async for event in shell.stream(str(tmp_path), "hello", resources=SessionResources(
        include_instruction_files=False,
    ))]

    # Assert
    command = host.calls[0][0]
    assert flag in command
    if value:
        assert value in command


@pytest.mark.parametrize("agent", [
    AgentType.OPENCODE, AgentType.COPILOT_CLI, AgentType.CODEX, AgentType.CURSOR, AgentType.GROK,
])
@pytest.mark.parametrize("mode", ["stream", "interactive"])
async def test_unavailable_exact_mcp_selection_fails_before_launch(agent, mode, tmp_path):
    # Arrange
    host = InteractiveHost() if mode == "interactive" else CaptureHost()
    shell = AgentShell(agent, execution_host=host)
    resources = SessionResources(mcp_servers=[])

    # Act / Assert
    with pytest.raises(NotImplementedError, match="mcp_servers"):
        if mode == "interactive":
            await shell.open_interactive(str(tmp_path), resources=resources)
        else:
            _ = [event async for event in shell.stream(str(tmp_path), "hi", resources=resources)]
    assert host.calls == []


async def test_unwritable_selection_store_fails_before_agent_launch(tmp_path, monkeypatch):
    # Arrange
    root = tmp_path / "readonly-state"
    root.mkdir()
    root.chmod(0o500)
    monkeypatch.setenv("XDG_STATE_HOME", str(root))
    host = CaptureHost("new-session")
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    try:
        with pytest.raises(OSError):
            await shell.execute(str(tmp_path), "hello", resources=SessionResources())
        assert host.calls == []
    finally:
        root.chmod(0o700)


@pytest.fixture
def missing_home(monkeypatch):
    def unavailable_home():
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", staticmethod(unavailable_home))


@pytest.mark.parametrize("mode", ["execute", "stream", "interactive"])
async def test_fresh_unselected_launch_needs_no_home_or_selection_store(
    tmp_path, monkeypatch, missing_home, mode,
):
    # Arrange
    monkeypatch.delenv("XDG_STATE_HOME")
    host = InteractiveHost() if mode == "interactive" else CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act
    if mode == "interactive":
        async with await shell.open_interactive(str(tmp_path)):
            pass
    elif mode == "stream":
        _ = [event async for event in shell.stream(str(tmp_path), "hello")]
    else:
        await shell.execute(str(tmp_path), "hello")

    # Assert
    assert len(host.calls) == 1
    assert not (tmp_path / "state").exists()


async def test_explicit_state_directory_allows_selection_and_resume_without_home(
    tmp_path, missing_home,
):
    # Arrange: the resource_state fixture supplies XDG_STATE_HOME.
    host = CaptureHost(session_id="homeless-session")
    resources = SessionResources(mcp_servers=[])

    # Act
    await AgentShell(AgentType.PI, execution_host=host).execute(
        str(tmp_path), "hello", resources=resources,
    )
    resumed = AgentShell(AgentType.PI, execution_host=host)
    await resumed.execute(
        str(tmp_path), "resume", session_id="homeless-session", resources=resources,
    )

    # Assert
    with pytest.raises(ValueError, match="original resource selection"):
        await resumed.execute(str(tmp_path), "resume", session_id="homeless-session")
    assert len(host.calls) == 2


async def test_package_install_without_home_or_cache_gives_actionable_error(
    tmp_path, monkeypatch, missing_home,
):
    # Arrange
    from agent_shell.models.agent import PackageSpec

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    with pytest.raises(ValueError, match="Set XDG_CACHE_HOME"):
        await shell.execute(str(tmp_path), "hello", resources=SessionResources(
            packages=[PackageSpec("npm:tools@1.2.3")],
        ))
    assert host.calls == []


@pytest.mark.parametrize("session_id,resources", [
    (None, SessionResources()),
    ("existing", None),
    ("existing", SessionResources()),
])
@pytest.mark.parametrize("state_override", [None, ""])
async def test_missing_state_and_home_gives_actionable_prelaunch_error(
    tmp_path, monkeypatch, missing_home, session_id, resources, state_override,
):
    # Arrange
    if state_override is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", state_override)
    host = CaptureHost()
    shell = AgentShell(AgentType.PI, execution_host=host)

    # Act / Assert
    with pytest.raises(ValueError, match="Set XDG_STATE_HOME"):
        await shell.execute(str(tmp_path), "hello", session_id=session_id, resources=resources)
    assert host.calls == []
