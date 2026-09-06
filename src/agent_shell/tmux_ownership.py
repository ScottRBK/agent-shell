"""Shared tmux resource identity. Kept stdlib-only for standalone workers."""

from dataclasses import dataclass
from pathlib import Path
import shlex


IDENTITY_FORMAT = "#{session_id}\t#{window_id}\t#{pane_id}\t#{socket_path}"


@dataclass(frozen=True)
class TmuxResource:
    kind: str
    session_id: str
    window_id: str
    pane_id: str
    socket_path: str
    marker: str

    @classmethod
    def from_identity(cls, kind: str, identity: str, directory: str | Path):
        fields = identity.strip().split("\t")
        if len(fields) != 4 or any(
            not value.startswith(prefix) or not value[1:].isdigit()
            for value, prefix in zip(fields[:3], ("$", "@", "%"))
        ) or not fields[3]:
            raise ValueError("tmux did not report valid session, window, pane and socket IDs")
        return cls(kind, *fields, Path(directory).name)

    def cleanup_args(self) -> list[str]:
        target = {"session": self.session_id, "window": self.window_id, "pane": self.pane_id}
        # The unique run directory is already in the pane's creation command. Check it on
        # the server before deleting: numeric IDs may be reused after a server restart.
        # No shell is involved in either the check or the conditional tmux command.
        return [
            "-S", self.socket_path, "if-shell", "-F", "-t", self.pane_id,
            "#{m:*" + self.marker + "*,#{pane_start_command}}",
            f"kill-{self.kind} -t {shlex.quote(target[self.kind])}",
        ]
