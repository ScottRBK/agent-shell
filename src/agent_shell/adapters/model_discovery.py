import asyncio
import os

from agent_shell.process_cleanup import create_grouped_process, kill_process_group


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


async def stop_model_processes(process: asyncio.subprocess.Process) -> None:
    """Ask the exact guardian to kill the discovery group, then reap the CLI."""
    kill_process_group(process)
    await _reap_process(process)


async def start_model_process(
    command: list[str],
    cwd: str,
    *,
    env: dict[str, str] | None = None,
    stdin: int = asyncio.subprocess.DEVNULL,
) -> asyncio.subprocess.Process:
    """Start a discovery CLI in a group owned by an exact guardian handle."""
    return await create_grouped_process(
        command,
        cwd=os.path.abspath(cwd),
        env=env,
        stdin=stdin,
    )


async def run_model_command(
    command: list[str],
    cwd: str,
    timeout: float,
    *,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a short-lived model-discovery command and clean up its whole group."""
    process = await start_model_process(
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
        await stop_model_processes(process)
        rendered = " ".join(command)
        raise RuntimeError(
            f"`{rendered}` timed out after {timeout:g} seconds"
        ) from error
    except BaseException:
        await stop_model_processes(process)
        raise

    returncode = process.returncode
    await stop_model_processes(process)
    return returncode, stdout, stderr
