"""Pi's transient package cache. Runtime authentication stays in Pi's normal home."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path

from agent_shell.execution import NativeExecutionHost
from agent_shell.adapters.stderr_format import format_stderr


@asynccontextmanager
async def _package_lock(path: Path):
    # flock releases on cancellation/process exit and coordinates separate Python processes.
    import fcntl

    with await asyncio.to_thread(path.open, "a", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def _command(arguments: list[str], home: Path) -> str:
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(home), "NO_COLOR": "1",
           "FORCE_COLOR": "0", "PI_TELEMETRY": "0"}
    process = await NativeExecutionHost().launch(
        ["pi", *arguments, "--no-approve"], cwd=str(home), env=env,
    )
    try:
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = format_stderr(stderr or stdout) or f"exit code {process.returncode}"
            raise RuntimeError(f"Pi session package {arguments[0]} failed: {detail}")
        return stdout.decode("utf-8", errors="strict")
    finally:
        await process.cancel()


def _listed_package(output: str, source: str, home: Path) -> Path:
    # Pi 1.2's list surface prints source followed by an indented installed path. Fail closed
    # on a changed format or a missing install, even if `pi install` happened to exit zero.
    lines = output.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line == f"  {source}" and lines[index + 1].startswith("    "):
            path = Path(lines[index + 1][4:])
            if path.is_dir() and path.resolve().is_relative_to(home.resolve()):
                return path
    raise RuntimeError("Pi did not report an installed session package in its private cache")


async def install_session_package(source: str) -> str:
    """Reuse a complete pinned install; never call the user's persistent package API."""
    configured_cache = os.environ.get("XDG_CACHE_HOME")
    try:
        cache = Path(configured_cache).expanduser() if configured_cache else Path.home() / ".cache"
    except RuntimeError as error:
        raise ValueError(
            "Cannot locate the Pi session package cache. "
            "Set XDG_CACHE_HOME to a writable directory."
        ) from error
    cache = cache.absolute() / "agent-shell" / "pi-packages"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(source.encode()).hexdigest()
    home = cache / key
    try:
        async with asyncio.timeout(120):
            async with _package_lock(cache / f"{key}.lock"):
                home.mkdir(exist_ok=True, mode=0o700)
                ready = home / "ready.json"
                try:
                    installed = json.loads(ready.read_text())
                    path = Path(installed["path"])
                    if (installed["source"] == source and path.is_dir()
                            and path.resolve().is_relative_to(home.resolve())):
                        return str(path)
                except (FileNotFoundError, ValueError, KeyError, TypeError):
                    pass
                await _command(["install", source], home)
                settings = json.loads((home / "settings.json").read_text())
                if source not in settings.get("packages", []):
                    raise RuntimeError("Pi session package install did not persist in its cache")
                path = _listed_package(await _command(["list"], home), source, home)
                temporary = home / "ready.tmp"
                temporary.write_text(json.dumps({"source": source, "path": str(path)}))
                temporary.replace(ready)
                return str(path)
    except TimeoutError as error:
        raise RuntimeError("Pi session package preparation timed out after 120 seconds") from error
