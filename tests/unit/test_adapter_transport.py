"""Transport-level regression tests shared across every adapter.

All six adapters share the same subprocess streaming boilerplate: a
`while True: chunk = await process.stdout.read(65536)` loop that decodes and
splits NDJSON, followed by a post-loop stderr read. Three latent bugs live in
that shared block, so they are guarded here ONCE, parametrized across all
adapters, rather than copy-pasted into each per-adapter suite:

  1. A multibyte UTF-8 character can be split across two 64KB reads. Per-chunk
     `chunk.decode("utf-8")` raises UnicodeDecodeError on the broken half and
     aborts the whole run (losing all text + result accounting).
  2. stderr is read only AFTER stdout is fully drained, so a child that fills
     its stderr pipe buffer mid-run deadlocks (it blocks writing stderr, never
     closes stdout, and our stdout read() waits forever).
  3. The error message built from stderr kept only the last 500 bytes, so a
     CLI that puts its reason at the *front* of a long stderr (cursor-agent's
     "Cannot use this model: <name>" followed by the full model list) had the
     reason silently dropped.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_shell.adapters.claude_code_adapter import ClaudeCodeAdapter
from agent_shell.adapters.codex_adapter import CodexAdapter
from agent_shell.adapters.copilot_cli_adapter import CopilotCLIAdapter
from agent_shell.adapters.opencode_adapter import OpenCodeAdapter
from agent_shell.adapters.pi_adapter import PiAdapter
from agent_shell.adapters.cursor_adapter import CursorAdapter


# Per-adapter builder for the one NDJSON event that carries assistant text, so a
# given string ends up in AgentResponse.response. Lets the transport tests stay
# adapter-agnostic while feeding each its own event shape.
def _claude_text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _codex_text(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def _opencode_text(text: str) -> dict:
    return {"type": "text", "part": {"text": text}}


def _copilot_text(text: str) -> dict:
    return {"type": "assistant.message", "data": {"content": text, "toolRequests": []}}


def _pi_text(text: str) -> dict:
    return {"type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "contentIndex": 1, "content": text}}


def _cursor_text(text: str) -> dict:
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


ADAPTERS = [
    pytest.param(ClaudeCodeAdapter, _claude_text, id="claude"),
    pytest.param(CodexAdapter, _codex_text, id="codex"),
    pytest.param(OpenCodeAdapter, _opencode_text, id="opencode"),
    pytest.param(CopilotCLIAdapter, _copilot_text, id="copilot"),
    pytest.param(PiAdapter, _pi_text, id="pi"),
    pytest.param(CursorAdapter, _cursor_text, id="cursor"),
]


def _process_with_stdout(chunks: list[bytes], stderr: bytes = b"", returncode: int = 0):
    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=list(chunks))
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    process.returncode = returncode
    process.wait = AsyncMock()
    process.pid = 4242
    return process


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_multibyte_char_split_across_reads_survives(adapter_cls, text_event):
    # Arrange — split the encoded stream right before a UTF-8 continuation byte so the first
    # chunk ends mid-character. ensure_ascii=False keeps the raw multibyte bytes in the stream.
    adapter = adapter_cls()
    text = "café ☕ 🎉 こんにちは"
    encoded = (json.dumps(text_event(text), ensure_ascii=False) + "\n").encode("utf-8")
    split = next(i for i in range(1, len(encoded)) if encoded[i] & 0xC0 == 0x80)
    process = _process_with_stdout([encoded[:split], encoded[split:], b""])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        response = await adapter.execute(cwd="/tmp", prompt="x")

    # Assert — the character survives intact rather than the run aborting on UnicodeDecodeError.
    assert text in response.response


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_byte_at_a_time_reads_survive(adapter_cls, text_event):
    # Arrange — one byte per read makes EVERY continuation byte of every multibyte width
    # (2/3/4-byte) cross a read boundary: the strongest cross-boundary stitching check.
    adapter = adapter_cls()
    text = "é ☕ 🎉"
    encoded = (json.dumps(text_event(text), ensure_ascii=False) + "\n").encode("utf-8")
    process = _process_with_stdout([bytes([b]) for b in encoded] + [b""])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        response = await adapter.execute(cwd="/tmp", prompt="x")

    # Assert
    assert text in response.response


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_final_line_without_trailing_newline_is_parsed(adapter_cls, text_event):
    # Arrange — a CLI may emit its last JSON object with no trailing newline; it is recovered
    # only via the EOF-branch json.loads(buffer), not the in-loop newline split.
    adapter = adapter_cls()
    text = "tail-no-newline"
    encoded = json.dumps(text_event(text), ensure_ascii=False).encode("utf-8")  # no "\n"
    process = _process_with_stdout([encoded, b""])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        response = await adapter.execute(cwd="/tmp", prompt="x")

    # Assert
    assert text in response.response


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_truncated_multibyte_at_eof_does_not_abort(adapter_cls, text_event):
    # Arrange — a run killed mid-write can end on an incomplete multibyte sequence with no
    # newline. A strict decoder would raise on the EOF flush; the "replace" incremental decoder
    # must let the run finish, keeping the already-parsed output. A lone lead byte (0xC3) at
    # true EOF is the trigger.
    adapter = adapter_cls()
    text = "before-truncation"
    good_line = (json.dumps(text_event(text), ensure_ascii=False) + "\n").encode("utf-8")
    process = _process_with_stdout([good_line, b"\xc3", b""])

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        response = await adapter.execute(cwd="/tmp", prompt="x")

    # Assert — the run completed (no UnicodeDecodeError) and earlier output survived.
    assert text in response.response


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_invalid_utf8_on_stderr_error_path_survives(adapter_cls, text_event):
    # Arrange — on a failed run the adapter decodes stderr to build the error event. Invalid
    # bytes there (a raw path fragment, terminal escape) must not raise UnicodeDecodeError and
    # mask the failure; errors="replace" turns them into U+FFFD.
    adapter = adapter_cls()
    process = _process_with_stdout([b""], stderr=b"boom \xff\xfe", returncode=1)

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        events = [event async for event in adapter.stream(cwd="/tmp", prompt="x")]

    # Assert
    errors = [e for e in events if e.type == "error"]
    assert errors and "boom" in errors[0].content


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_front_loaded_stderr_reason_survives_long_tail(adapter_cls, text_event):
    # Arrange — reason at the very start of stderr, followed by ~4KB of noise, mirrors
    # cursor-agent's "Cannot use this model: <name>" + full model list on a bad model.
    # A tail-only truncation drops the reason entirely.
    adapter = adapter_cls()
    reason = "Cannot use this model: bogus."
    stderr = (reason + " " + "x" * 4000).encode("utf-8")
    process = _process_with_stdout([b""], stderr=stderr, returncode=1)

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        events = [event async for event in adapter.stream(cwd="/tmp", prompt="x")]

    # Assert
    errors = [e for e in events if e.type == "error"]
    assert errors and reason in errors[0].content


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_early_close_cancels_stderr_drain_task(adapter_cls, text_event):
    # Arrange — the concurrent stderr drain is started before the stdout loop. If a consumer
    # breaks the async-for early, the generator must cancel that task instead of orphaning it
    # (which surfaces as "Task was destroyed but it is pending"). `never` models a still-alive
    # child whose pipes have not reached EOF.
    adapter = adapter_cls()
    pending = [(json.dumps(text_event("hi"), ensure_ascii=False) + "\n").encode("utf-8")]
    never = asyncio.Event()

    async def stdout_read(_n=-1):
        if pending:
            return pending.pop(0)
        await never.wait()
        return b""

    async def stderr_read(_n=-1):
        await never.wait()
        return b""

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = stdout_read
    process.stderr = MagicMock()
    process.stderr.read = stderr_read
    process.returncode = 0
    process.wait = AsyncMock()
    process.pid = 4242

    before = set(asyncio.all_tasks())

    # Act — take the first event, then close the generator early.
    with patch("asyncio.create_subprocess_exec", return_value=process):
        agen = adapter.stream(cwd="/tmp", prompt="x")
        first = await agen.__anext__()
        assert first.type == "text"
        await agen.aclose()

    await asyncio.sleep(0.05)  # let the requested cancellation finalize

    # Assert — no drain task left pending.
    leaked = [t for t in asyncio.all_tasks() if t not in before and not t.done()]
    assert leaked == [], f"stderr drain task orphaned on early close: {leaked}"


@pytest.mark.parametrize("adapter_cls,text_event", ADAPTERS)
async def test_stderr_drained_concurrently_with_stdout(adapter_cls, text_event):
    # Arrange — gate stdout's EOF on stderr having been read. The old "read stderr after the
    # stdout loop" order would hang here (stdout_read times out); concurrent draining completes.
    adapter = adapter_cls()
    pending = [(json.dumps(text_event("hi"), ensure_ascii=False) + "\n").encode("utf-8")]
    stderr_drained = asyncio.Event()

    async def stdout_read(_n=-1):
        if pending:
            return pending.pop(0)
        await asyncio.wait_for(stderr_drained.wait(), timeout=2.0)
        return b""

    async def stderr_read(_n=-1):
        stderr_drained.set()
        return b""

    process = AsyncMock()
    process.stdout = MagicMock()
    process.stdout.read = stdout_read
    process.stderr = MagicMock()
    process.stderr.read = stderr_read
    process.returncode = 0
    process.wait = AsyncMock()
    process.pid = 4242

    # Act
    with patch("asyncio.create_subprocess_exec", return_value=process):
        events = [event async for event in adapter.stream(cwd="/tmp", prompt="x")]

    # Assert — completing at all proves stderr was drained while the stdout loop ran.
    assert any(e.type == "text" for e in events)
