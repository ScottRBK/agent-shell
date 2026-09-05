"""Launch real harness UIs; this console only controls sessions and reports observed events.

Run: uv run python examples/interactive_demo.py --agent all
Attach using the printed tmux command from another terminal. No prompt is sent by default.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import shlex
import signal
import sys

from agent_shell import TmuxExecutionHost, TmuxPlacement, discover_terminal_launcher
from agent_shell.models.agent import AgentType
from agent_shell.shell import AgentShell


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=["all", *[agent.value for agent in AgentType]],
                        default="all")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--model", help="Harness-native model selector; requires a single --agent")
    parser.add_argument("--prompt", help="Optional initial prompt (makes a live model request)")
    parser.add_argument("--new-terminal", action="store_true",
                        help="Open a terminal emulator attached to the real tmux session")
    args = parser.parse_args()
    if args.model and args.agent == "all":
        parser.error("--model requires a single --agent")
    return args


async def report_events(name, session):
    async for event in session.events():
        details = f"[{name}] {event.type}: {event.content}"
        if event.session_id:
            details += f" (session {event.session_id})"
        if event.type == "result" and "output_tokens" in session.capabilities:
            details += f"; output tokens={event.output_tokens}, cost=${event.cost:.4f}"
        if event.error:
            details += f"; {event.error}"
        print(details, flush=True)


async def main(args):
    names = [agent.value for agent in AgentType] if args.agent == "all" else [args.agent]
    sessions = {}
    observers = []
    loop = asyncio.get_running_loop()
    commands = asyncio.Queue()

    def read_command():
        line = sys.stdin.readline()
        if not line:
            loop.remove_reader(sys.stdin)
            commands.put_nowait("/quit")
        else:
            commands.put_nowait(line.rstrip("\n"))

    async with AsyncExitStack() as stack:
        for name in names:
            placement = (
                TmuxPlacement.new_session() if not sessions
                else TmuxPlacement.new_window(next(iter(sessions.values())).terminal.session_name)
            )
            shell = AgentShell(AgentType(name), execution_host=TmuxExecutionHost(placement))
            session = await shell.open_interactive(
                str(Path(args.cwd).resolve()), model=args.model, prompt=args.prompt,
            )
            await stack.enter_async_context(session)
            sessions[name] = session
            print(f"{name}: real UI in pane {session.terminal.pane_id}; "
                  f"events: {', '.join(sorted(session.capabilities))}", flush=True)

        target = next(iter(sessions.values())).terminal.session_name
        attach = ["tmux", "attach-session", "-t", target]
        print("From another terminal: " + shlex.join(attach), flush=True)
        print("Inside tmux: " + shlex.join(["tmux", "switch-client", "-t", target]), flush=True)
        print("Use tmux next-window (prefix+n) to move between the real harness UIs.", flush=True)
        if args.new_terminal:
            launcher = discover_terminal_launcher()
            await launcher.launch(attach, cwd=args.cwd, env=os.environ.copy())

        for name, session in sessions.items():
            observers.append(asyncio.create_task(report_events(name, session)))
        loop.add_reader(sys.stdin, read_command)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, commands.put_nowait, "/quit")
        try:
            print("Controller ready. Resolve trust/login prompts in the real UI first.", flush=True)
            print("Send: <agent> <prompt> | /key <agent> C-c | /quit", flush=True)
            while (line := await commands.get()) != "/quit":
                try:
                    if line.startswith("/key "):
                        _, name, key = line.split(maxsplit=2)
                        await sessions[name].terminal.send_key(key)
                    else:
                        name, text = line.split(maxsplit=1)
                        await sessions[name].terminal.send_text(text, submit=True)
                except (KeyError, ValueError, RuntimeError) as error:
                    print(f"Could not send input: {error}", flush=True)
        finally:
            loop.remove_reader(sys.stdin)
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            for observer in observers:
                observer.cancel()
            await asyncio.gather(*observers, return_exceptions=True)
    print("Closed all demo sessions", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main(arguments()))
    except KeyboardInterrupt:
        pass
