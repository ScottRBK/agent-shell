import asyncio
import os
import sys

from agent_shell.process_cleanup import kill_process_group, register_process_group


_GROUP_LEADER = "import signal\nwhile True: signal.pause()"


def decode_model_output(output: bytes, source: str) -> str:
    """Decode model metadata without leaking a low-level codec error."""
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{source} returned invalid UTF-8 output") from error


async def _reap_process(process: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except (TimeoutError, ProcessLookupError):
        pass


async def stop_model_processes(
    process: asyncio.subprocess.Process,
    group_leader: asyncio.subprocess.Process,
) -> None:
    """Kill the isolated discovery group through its still-live leader, then reap it."""
    kill_process_group(group_leader.pid)
    await asyncio.gather(
        _reap_process(process),
        _reap_process(group_leader),
    )


async def start_model_process(
    command: list[str],
    cwd: str,
    *,
    env: dict[str, str] | None = None,
    stdin: int = asyncio.subprocess.DEVNULL,
) -> tuple[asyncio.subprocess.Process, asyncio.subprocess.Process]:
    """Start a CLI in a group whose separate leader stays alive until cleanup."""
    absolute_cwd = os.path.abspath(cwd)
    group_leader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        _GROUP_LEADER,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=absolute_cwd,
        env=env,
        process_group=0,
    )
    register_process_group(group_leader.pid)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=absolute_cwd,
            env=env,
            process_group=group_leader.pid,
        )
    except BaseException:
        kill_process_group(group_leader.pid)
        await _reap_process(group_leader)
        raise

    return process, group_leader


async def run_model_command(
    command: list[str],
    cwd: str,
    timeout: float,
    *,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a short-lived model-discovery command and clean up its whole group."""
    process, group_leader = await start_model_process(
        command,
        cwd,
        env=env,
        stdin=(
            asyncio.subprocess.PIPE
            if input_data is not None
            else asyncio.subprocess.DEVNULL
        ),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_data),
            timeout=timeout,
        )
    except TimeoutError as error:
        await stop_model_processes(process, group_leader)
        rendered = " ".join(command)
        raise RuntimeError(
            f"`{rendered}` timed out after {timeout:g} seconds"
        ) from error
    except BaseException:
        await stop_model_processes(process, group_leader)
        raise

    returncode = process.returncode
    await stop_model_processes(process, group_leader)
    return returncode, stdout, stderr
