"""Shared framed IPC protocol for the tmux host and its one-shot worker."""

from __future__ import annotations

import asyncio
import struct

# One byte channel, followed by an unsigned network-order payload length and the payload itself.
FRAME_HEADER = struct.Struct("!BI")
MAX_FRAME = 64 * 1024 * 1024

HELLO = 1
CONFIG = 2
STDOUT = 3
STDERR = 4
EXIT = 5
ERROR = 6
RELEASE = 7
STDIN = 8
CLOSE_STDIN = 9
CANCEL = 10


class TmuxProtocolError(RuntimeError):
    """Raised when framed IPC data is malformed or exceeds the protocol limit."""


async def send_frame(
    writer: asyncio.StreamWriter,
    channel: int,
    payload: bytes,
) -> None:
    if len(payload) > MAX_FRAME:
        raise TmuxProtocolError("tmux bridge frame is too large")
    writer.write(FRAME_HEADER.pack(channel, len(payload)) + payload)
    await writer.drain()


async def receive_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(FRAME_HEADER.size)
    channel, size = FRAME_HEADER.unpack(header)
    if size > MAX_FRAME:
        raise TmuxProtocolError("tmux bridge frame is too large")
    return channel, await reader.readexactly(size)
