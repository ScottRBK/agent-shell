# Interactive harness review

Grok reviewed this feature through `AgentShell(AgentType.GROK).open_interactive(...)` in its
actual tmux UI, using native read/search-only tool selection. No app server or headless
rendering was used. The same conversation was resumed for its verdict and targeted re-review.
The optional Grok OS sandbox could not start because bubblewrap was absent; the review used
Grok's native tool whitelist, not an OS isolation guarantee.

Initial verdict: **REQUEST_CHANGES**. Follow-up verdict: **APPROVE**.

| Finding | Disposition |
|---|---|
| Cursor optional resume flag binding | Fixed `--resume=ID`; regression rejects separate tokens. |
| Immediate child exit before foreground handoff | Withdrawn after Linux zombie/handoff probe. |
| tmux kill timeout leaked ownership | Fixed with unconditional FIFO release and cleanup. |
| Pi text/tool events lacked session IDs | Fixed; regression checks every emitted event. |

The cleanup regression deliberately hangs the external tmux kill command, then proves the owned
session disappears while its controller is still alive. A real `/bin/true` regression protects
immediate exit handling. The worker does not reap the child before foreground handoff.

The follow-up was scoped to the four findings. It confirmed all three fixes and withdrew the
fast-exit claim. Review does not imply full metadata parity: limitations are documented in
[the POC guide](interactive-harness-poc.md).

After review, a real Cursor resume check revealed that `sessionStart` does not fire on resume.
The adapter now advertises no structured capabilities for resumed Cursor conversations, while
retaining native UI control. A live recall test checks the requested conversation against a
newer decoy session.

Validation: 1,009 unit/integration tests passed. Six harnesses passed real prompt E2E checks.
Claude's live check was blocked by a pre-existing settings error; a later Pi rerun was blocked
by an installed extension's runtime-setup prompt. Neither user configuration was bypassed.
