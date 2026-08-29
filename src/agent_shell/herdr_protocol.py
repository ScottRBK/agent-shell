"""Private framed transport shared by the Herdr host and its one-shot worker."""

from __future__ import annotations

import asyncio
import struct

FRAME_HEADER = struct.Struct("!BI")
MAX_FRAME_SIZE = 64 * 1024 * 1024

HELLO = b"H"
BRIDGE_CONFIG = b"G"
LAUNCH = b"L"
LAUNCH_READY = b"A"
LAUNCH_ERROR = b"F"
STDOUT = b"O"
STDERR = b"E"
EXIT = b"X"
STDIN = b"I"
STDIN_EOF = b"Q"
CANCEL = b"C"
RELEASE = b"R"


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read one bounded kind/payload frame from the private Unix socket."""
    header = await reader.readexactly(FRAME_HEADER.size)
    kind, length = FRAME_HEADER.unpack(header)
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"Herdr bridge frame is too large: {length} bytes")
    return bytes((kind,)), await reader.readexactly(length)


async def write_frame(
    writer: asyncio.StreamWriter,
    kind: bytes,
    payload: bytes = b"",
) -> None:
    """Write one kind/payload frame and wait until it reaches the socket buffer."""
    if len(kind) != 1:
        raise ValueError("Herdr bridge frame kinds must be one byte")
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError(f"Herdr bridge frame is too large: {len(payload)} bytes")
    writer.write(FRAME_HEADER.pack(kind[0], len(payload)) + payload)
    await writer.drain()
