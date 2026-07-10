"""Unit tests for the shared stderr-truncation helper.

`format_stderr` is the single implementation every adapter delegates to when
building the error message for a non-zero exit. CLIs vary in whether the
actionable reason leads (Cursor: reason first, then a long model list) or
trails (stack-trace style); a tail-only slice silently drops the former, so
the helper keeps both ends of long stderr.
"""

from agent_shell.adapters.stderr_format import format_stderr


class TestFormatStderr:
    def test_short_stderr_is_returned_unchanged(self):
        # Arrange
        stderr = b"boom: connection refused"

        # Act
        result = format_stderr(stderr)

        # Assert
        assert result == "boom: connection refused"

    def test_front_loaded_reason_survives_a_long_tail(self):
        # Arrange — reason at the very start, followed by ~4KB of noise, mirrors the
        # real cursor-agent "Cannot use this model: <name>" + full model list case.
        reason = "Cannot use this model: bogus."
        noise = "x" * 4000
        stderr = f"{reason} {noise}".encode()

        # Act
        result = format_stderr(stderr)

        # Assert
        assert reason in result

    def test_trailing_reason_survives_a_long_head(self):
        # Arrange — stack-trace style: reason at the very end, preceded by ~4KB of noise.
        reason = "RuntimeError: config not found"
        noise = "x" * 4000
        stderr = f"{noise} {reason}".encode()

        # Act
        result = format_stderr(stderr)

        # Assert
        assert reason in result

    def test_long_stderr_is_shorter_than_the_original(self):
        # Arrange
        stderr = b"x" * 10_000

        # Act
        result = format_stderr(stderr)

        # Assert
        assert len(result) < 10_000

    def test_invalid_utf8_does_not_raise(self):
        # Arrange
        stderr = b"boom \xff\xfe"

        # Act
        result = format_stderr(stderr)

        # Assert
        assert "boom" in result
