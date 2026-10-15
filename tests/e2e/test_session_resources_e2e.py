"""Real Pi resource loading without inference; fixtures use a disposable native home."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from agent_shell.models.agent import AgentType, PackageSpec
from agent_shell.models.resources import SessionResources
from agent_shell.shell import AgentShell

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not shutil.which("pi"), reason="Pi CLI required")]


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def marker(path, name):
    write(path, f'export default pi => pi.registerCommand("{name}", '
                '{description:"test marker",handler:async()=>{}});')


async def test_concurrent_pi_roles_exclude_home_resources_keep_context_and_default_prompt(
    tmp_path, monkeypatch,
):
    # Arrange
    home = tmp_path / "personal"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    monkeypatch.setenv("PI_OFFLINE", "1")
    monkeypatch.setenv("PI_TELEMETRY", "0")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    write(home / "settings.json", '{}')
    write(project / ".pi/settings.json", '{}')
    write(home / "SYSTEM.md", "PERSONAL_SYSTEM_MARKER")
    write(home / "APPEND_SYSTEM.md", "PERSONAL_APPEND_MARKER")
    write(home / "AGENTS.md", "PERSONAL_CONTEXT_MARKER")
    write(project / "AGENTS.md", "PROJECT_CONTEXT_MARKER")
    marker(home / "extensions/personal.js", "marker-personal")
    marker(project / ".pi/extensions/project.js", "marker-project")
    settings = [home / "settings.json", project / ".pi/settings.json"]
    before = [path.read_bytes() for path in settings]
    selections = []
    outputs = []
    for role in ("foreman", "owner"):
        package = tmp_path / role
        marker(package / "extensions/role.js", f"marker-{role}")
        write(package / "skills/role/SKILL.md",
              f"---\nname: marker-skill-{role}\ndescription: marker\n---\nrole")
        write(package / "package.json", json.dumps({"name": role, "pi": {
            "extensions": ["./extensions/role.js"], "skills": ["./skills/role"],
        }}))
        output = tmp_path / f"{role}.json"
        observer = tmp_path / f"{role}-observer.mjs"
        write(observer, '''import {writeFileSync} from "node:fs";
export default pi => {
  pi.on("input", (event, ctx) => {
    writeFileSync(OUTPUT, JSON.stringify({
      commands: pi.getCommands().map(c => c.name).filter(n => n.includes("marker")).sort(),
      prompt: ctx.getSystemPrompt(),
    }));
    return {action:"handled"};
  });
};
'''.replace("OUTPUT", json.dumps(str(output))))
        selections.append(SessionResources(
            packages=[PackageSpec(str(package))], extensions=[str(observer)], mcp_servers=[],
            inherit_personal_resources=False, inherit_project_resources=False,
            include_home_system_prompt=False,
        ))
        outputs.append(output)
    shell = AgentShell(AgentType.PI)

    # Act
    async def run(selection):
        return [event async for event in shell.stream(str(project), "probe", resources=selection)]

    async with asyncio.timeout(45):
        events = await asyncio.gather(*(run(selection) for selection in selections))

    # Assert: observations are emitted by the selected extension through Pi's public API.
    for role, output, stream in zip(("foreman", "owner"), outputs, events):
        result = json.loads(output.read_text())
        assert result["commands"] == [f"marker-{role}", f"skill:marker-skill-{role}"]
        assert "PERSONAL_SYSTEM_MARKER" not in result["prompt"]
        assert "PERSONAL_CONTEXT_MARKER" in result["prompt"]
        assert "PROJECT_CONTEXT_MARKER" in result["prompt"]
        assert "PERSONAL_APPEND_MARKER" in result["prompt"]
        assert "coding" in result["prompt"].lower()
        assert not [event for event in stream if event.type == "error"]
    assert [path.read_bytes() for path in settings] == before


async def test_native_pi_installs_selected_package_from_local_registry(tmp_path, monkeypatch):
    # Arrange: serve a tiny npm package locally, so native installation needs no public registry.
    import hashlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import io
    import os
    import tarfile
    import threading

    output = tmp_path / "installed-extension-ran"
    package = {
        "package.json": json.dumps({"name": "agentshell-resource-fixture", "version": "1.0.0",
                                    "pi": {"extensions": ["./extension.js"]}}),
        "extension.js": 'import {writeFileSync} from "node:fs";\n'
                        'export default pi => pi.on("input", event => {\n'
                        f'writeFileSync({json.dumps(str(output))}, event.text);\n'
                        'return {action:"handled"}; });',
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in package.items():
            data = content.encode()
            info = tarfile.TarInfo("package/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    tarball = buffer.getvalue()
    requests = []

    class Registry(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path.endswith(".tgz"):
                data = tarball
            else:
                data = json.dumps({
                    "name": "agentshell-resource-fixture", "dist-tags": {"latest": "1.0.0"},
                    "versions": {"1.0.0": {
                        "name": "agentshell-resource-fixture", "version": "1.0.0",
                        "dist": {
                            "tarball": f"http://127.0.0.1:{self.server.server_port}/package.tgz",
                                 "shasum": hashlib.sha1(tarball).hexdigest()}}},
                }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    personal = tmp_path / "personal"
    write(personal / "settings.json", '{}')
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(personal))
    monkeypatch.setenv("PI_TELEMETRY", "0")
    monkeypatch.delenv("PI_OFFLINE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("npm_config_registry", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "npm-cache"))
    write(tmp_path / "npmrc", "")
    monkeypatch.setenv("npm_config_userconfig", str(tmp_path / "npmrc"))
    resources = SessionResources(
        packages=[PackageSpec("npm:agentshell-resource-fixture@1.0.0")], mcp_servers=[],
        inherit_personal_resources=False, inherit_project_resources=False,
    )
    shell = AgentShell(AgentType.PI)

    try:
        # Act
        async with asyncio.timeout(60):
            first = [event async for event in shell.stream(
                str(tmp_path), "first", resources=resources,
            )]
            downloads = len(requests)
            second = [event async for event in shell.stream(
                str(tmp_path), "second", resources=resources,
            )]

        # Assert
        assert output.read_text() == "second"
        assert any(path.endswith(".tgz") for path in requests)
        assert len(requests) == downloads
        assert (personal / "settings.json").read_text() == '{}'
        assert os.environ["PI_CODING_AGENT_DIR"] == str(personal)
        assert not [event for event in [*first, *second] if event.type == "error"]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()
