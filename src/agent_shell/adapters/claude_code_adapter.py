import asyncio 
import json 
import os
from typing import AsyncIterator

from agent_shell.models.agent import AgentResponse, StreamEvent

class ClaudeCodeAdapter():
    def __init__(self):
        self._active_processes = []

    async def execute(
            self,
            cwd: str,
            prompt: str,
            allowed_tools: list[str] | None = None,
            model: str | None = None,
            include_thinking: bool = False,
    ) -> AgentResponse:
        chunks: list[StreamEvent] = []
        async for event in self.stream(
            cwd=cwd,
            prompt=prompt,
            allowed_tools=allowed_tools,
            model=model,
            include_thinking=include_thinking,
        ):
            chunks.append(event)

        text = "\n".join(e.content for e in chunks if e.type == "text")
        cost = next((e.cost for e in reversed(chunks) if e.type == "result"), 0.0)
        return AgentResponse(response=text, cost=cost)

    async def stream(
            self, 
            cwd: str, 
            prompt: str, 
            allowed_tools: list[str] | None = None, 
            model: str | None = None,
            include_thinking: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        cmd = [
            "claude", "-p", prompt,
           "--output-format", "stream-json",
            "--verbose"
        ]

        if allowed_tools:
            cmd.extend(["--allowed-tools", ",".join(allowed_tools)])

        if model:
            cmd.extend(["--model", model])

        process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.abspath(cwd),
                preexec_fn=os.setsid,
        )

        self._active_processes.append(process)

        buffer = ""
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                if buffer.strip():
                    try:
                        for event in self._parse_event(
                            event=json.loads(buffer),
                            include_thinking=include_thinking
                            ):
                                yield event
                    except json.JSONDecodeError:
                        pass
                break

            buffer += chunk.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    try:
                        for event in self._parse_event(
                            event=json.loads(line),
                            include_thinking=include_thinking):
                            yield event
                    except json.JSONDecodeError:
                        pass

        await process.wait()
        if process in self._active_processes:
            self._active_processes.remove(process)

        stderr = await process.stderr.read()
        if stderr and process.returncode != 0:
            yield StreamEvent(type="error", content=stderr.decode("utf-8")[-500:])

    def _parse_event(self, event: dict, include_thinking: bool) -> list[StreamEvent]:
        t = event.get("type", "")
        events = []

        if t == "assistant":
            for item in event.get("message", {}).get("content", []):
                if item.get("type") == "text":
                    events.append(StreamEvent(type="text", content=item["text"]))
                elif item.get("type") == "tool_use":
                    events.append(StreamEvent(type="tool_use", content=item.get("name", "")))
                elif item.get("type") == "thinking" and include_thinking:
                    events.append(StreamEvent(type="thinking", content=item.get("thinking", "")))

        elif t == "result":
            cost = event.get("total_cost_usd", 0) or 0
            duration = (event.get("duration_ms", 0) or 0) / 1000
            is_error = event.get("is_error", False)
            status = "error" if is_error else "ok"
            events.append(StreamEvent(type="result", content=status, cost=cost, duration=duration))

        return events 

    async def cancel(self) -> None:
          for process in self._active_processes:
              try:
                  os.killpg(os.getpgid(process.pid), 9)
              except ProcessLookupError:
                  pass
          self._active_processes.clear()

                




