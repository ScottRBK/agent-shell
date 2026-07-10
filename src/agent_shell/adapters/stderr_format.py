"""Shared stderr-truncation logic for all adapters.

Every adapter decodes a failed child's stderr into the `error` StreamEvent's content.
CLIs disagree on where the actionable reason sits in a long stderr: some lead with it
(cursor-agent: "Cannot use this model: <name>" followed by the full model list), others
trail with it (stack-trace style). A tail-only slice suits the latter but silently drops
the former, so this keeps both ends.
"""

ELISION_MARKER = "\n... [truncated] ...\n"


def format_stderr(stderr: bytes, head: int = 500, tail: int = 500) -> str:
    """Decode stderr for error reporting, preserving both its start and end."""
    text = stderr.decode("utf-8", errors="replace")
    if len(text) <= head + tail:
        return text
    return text[:head] + ELISION_MARKER + text[-tail:]
