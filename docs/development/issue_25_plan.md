# Issue #25: attach to an existing interactive terminal

## Scope

Add `AgentShell.attach_interactive(target)` without launching or resuming an agent process.
Reuse `.terminal.send_text(text, submit=True)`. Disconnect must leave the external process alive.
Opening and attaching are independent host capabilities; only tmux supports attachment initially.
No adapter changes, custom queues, new dependencies, or Pi-extension changes are included.

## Design

- `InteractiveConnection` owns only a connection, not the agent, and exposes terminal controls.
- Split terminal controls from the launched terminal's `wait()` and `returncode` contract.
- Add an optional `InteractiveAttachmentHost` protocol, separate from launch support.
- Add a frozen `TmuxTarget`, captured explicitly by pane ID or from the target pane's environment.
  Launched terminals expose their captured target too. Bind targets to the server and pane process
  identities so a server restart or pane respawn cannot silently redirect a saved connection.
- Share tmux input/screen transport between owned sessions and borrowed connections. Only owned
  sessions keep worker FIFOs, status files, stop requests, and destructive cleanup.
- Check identity on the server before each attached pane operation. Never fall back to launching.
- Attachment cannot apply an isolation policy retroactively. Reject non-`NoIsolation` policies
  explicitly rather than imply the external process is protected.
- Do not expose structured events or invented exit status on a borrowed connection.
- A target identifies a terminal, not a verified agent or conversation. The caller remains
  responsible for the foreground application, drafts, and readiness. Native queue behaviour belongs
  to the agent; our manual Codex test observed delivery after its next tool call.

## Approved test boundary and implementation slices

Scott approved the public attachment boundary and existing launched-session regression coverage.
Use real tmux on isolated test servers and controlled external processes, not inference calls.
Each slice starts with a failing public-behaviour test, followed by its implementation.

1. Attach to an externally launched process, submit literal text, disconnect, and reconnect to the
   same process. Keep existing launched-session behaviour passing while sharing transport.
2. Capture the current pane and expose a launched terminal's target. Reject unsupported hosts,
   mismatched targets, unavailable tmux, and unsupported isolation without launching anything.
3. Reject missing/dead/respawned panes and restarted servers, including during an existing
   connection. Exercise exceptions, cancellation, repeat close, and input after disconnect.
4. Update public exports, examples, architecture, and API limits. Run unit/integration regressions.
5. Independent Opus 5.5 review; reproduce and fix findings test-first, then rerun validation.

## Validation limits

The existing two manual Codex queue probes validated native busy-message queueing, not preservation
of a partly typed draft or queue behaviour for every agent. Automated tests should validate literal
terminal input and lifecycle ownership without pretending to prove those agent-specific behaviours.

## Validation results

- Unit/integration suite: 1,176 passed.
  Command: `.venv/bin/python -m pytest tests/unit tests/integration -q`.
- New public attachment and validation tests: 56 passed, including all seven adapter selections,
  external-process survival, mid-send cancellation, stale targets across all terminal controls,
  separate watcher environments, and compatibility with a CLI that rejects tmux 3.2's `-N`.
- Real Codex smoke test: 1 passed using `gpt-6-luna`; the public attachment delivered a follow-up
  and disconnect left the original interactive pane usable.
- Python compilation, `git diff --check`, and the changed files' 100-column check passed.
- Independent Opus 5.5 review (`opencode/claude-opus-5-5` through Pi): no blocking findings.
  Removed the unnecessary `-N` flag, extracted the shared tmux command helper, documented the
  tmux-specific target property, and added the suggested cancellation/stale-operation regressions.
  Kept `gpt-6-luna`: it was advertised by real Codex discovery and passed both live smoke runs.
  Focused follow-up: approved with no blockers; the reviewer ran 106 focused tests successfully.
  Final documentation line-wrap nits were corrected and the 100-column check rerun.
