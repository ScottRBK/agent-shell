"""Framing primitives shared by the terminal-window host and its worker."""

import asyncio
import struct

# The worker and owner exchange length-prefixed frames. The kind byte stays separate from
# payload bytes so stdout/stderr and stdin remain byte-preserving rather than text protocols.
_TERMINAL_FRAME_HEADER = struct.Struct("!I")
_TERMINAL_MAX_FRAME = 64 * 1024 * 1024
_TERMINAL_HELLO = b"H"
_TERMINAL_REQUEST = b"Q"
_TERMINAL_STDOUT = b"O"
_TERMINAL_STDERR = b"E"
_TERMINAL_STDIN = b"I"
_TERMINAL_STDIN_EOF = b"F"
_TERMINAL_CANCEL = b"C"
_TERMINAL_STATUS = b"S"
_TERMINAL_ERROR = b"X"


async def _read_terminal_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    header = await reader.readexactly(_TERMINAL_FRAME_HEADER.size)
    (length,) = _TERMINAL_FRAME_HEADER.unpack(header)
    if length < 1 or length > _TERMINAL_MAX_FRAME:
        raise ValueError(f"invalid terminal IPC frame length: {length}")
    payload = await reader.readexactly(length)
    return payload[:1], payload[1:]


async def _write_terminal_frame(
    writer: asyncio.StreamWriter,
    kind: bytes,
    payload: bytes = b"",
) -> None:
    if len(kind) != 1:
        raise ValueError("terminal IPC frame kind must be one byte")
    if len(payload) + 1 > _TERMINAL_MAX_FRAME:
        raise ValueError("terminal IPC frame is too large")
    writer.write(
        _TERMINAL_FRAME_HEADER.pack(len(payload) + 1) + kind + payload
    )
    await writer.drain()
