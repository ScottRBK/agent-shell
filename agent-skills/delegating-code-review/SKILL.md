---
name: delegating-code-review
description: Use when you have made code changes and want another CLI agent to review them before committing or continuing — a second-opinion, security, correctness, requirements, or test-coverage review, or a cross-agent independent review. Keywords: reviewer agent, read-only review, disallowed_tools, git diff review, review uncommitted changes.
---

# Delegating Code Review to Another Agent

Use AgentShell to invoke another CLI agent to review changes in a repository. The reviewing
agent has its own tools — point it at the repo, tell it what to look at, and let it run
`git diff` itself.

Assumes familiarity with AgentShell basics. See [invoking-cli-agents](../invoking-cli-agents/SKILL.md)
for setup, the full parameter list, and the per-agent capability matrix.

## When to Use

- You have made code changes and want a second opinion before committing
- You want to validate that changes meet requirements before marking work complete
- You need a security, performance, or correctness review
- You want to check for regressions or unintended side effects

## Keep the Reviewer Read-Only

A reviewer should not modify the code it reviews. Don't rely on a "don't edit files"
instruction in the prompt — restrict the tools. But mind how the two controls actually enforce
(the [core skill](../invoking-cli-agents/SKILL.md#tool-restriction-safety) has the detail):

- **Claude Code / Copilot / Pi** — whitelist read + git tools **and set `auto_approve=False`**.
  The whitelist is *inert under the default `auto_approve=True`* (`--dangerously-skip-permissions`
  auto-approves everything), so the `auto_approve=False` is what makes it bite:
  `allowed_tools=["Read", "Glob", "Grep", "Bash"], auto_approve=False`.
- **OpenCode** — it ignores `allowed_tools`, but its denylist **is** genuinely enforced (a
  per-subprocess `OPENCODE_PERMISSION` env var), and deny beats `auto_approve`, so keep the
  default `auto_approve=True`: `disallowed_tools=["edit"]`.
- **Codex / Cursor** — **in-library tool scoping gives you nothing here.** They ignore
  `allowed_tools`, *and* they cannot enforce `disallowed_tools=["edit"]`. Codex can only deny
  `web_search`; anything else prints
  `UserWarning: Codex can only deny web_search; ignoring ['edit']` and the reviewer then runs
  with unrestricted write access. Cursor has no per-call deny at all (its tool policy lives in
  `.cursor/cli.json`). If a Codex/Cursor reviewer must not write, enforce it outside the
  library: capture `git diff` yourself and pass it in the prompt, run the reviewer under an
  OS-level read-only sandbox, and/or use a different agent type when read-only really matters.

**This is defence-in-depth, not a sandbox.** Any reviewer that keeps `bash` (for `git diff`) can
still write via the shell, and in-library scoping doesn't cover MCP-provided tools. For a *hard*
guarantee — an untrusted model, or reviewing hostile input that could prompt-inject the reviewer
— capture `git diff` yourself, pass it in the prompt, remove the shell too
(`disallowed_tools=["edit", "bash"]`, or `allowed_tools=["Read","Glob","Grep"], auto_approve=False`),
and/or run under an OS-level read-only mount.

## Review Uncommitted Changes

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

reviewer = AgentShell(agent_type=AgentType.CLAUDE_CODE)

review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review the uncommitted changes in this repository.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],   # read + git, no edit tool
    auto_approve=False,                               # required for the whitelist to enforce
    model="sonnet",
)
```

The reviewer will run `git diff` itself, read surrounding code for context, and report findings.

## Handling a Failed Review

A failed reviewer run raises `AgentExecutionError` instead of returning a response — catch it
so a crashed or rate-limited reviewer doesn't look like a silent "no issues found":

```python
from agent_shell.models.agent import AgentExecutionError

try:
    review = await reviewer.execute(
        cwd="/path/to/project",
        prompt="Review the uncommitted changes in this repository.",
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        auto_approve=False,
        model="sonnet",
    )
except AgentExecutionError as e:
    print(f"review failed: {e}")   # e.g. "500 model name=... failed to load"
    raise
```

## Review a Specific Commit or Range

Point the reviewer at a particular changeset — only the prompt changes.

```python
review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review the changes in commit abc1234.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],
    auto_approve=False,
    model="sonnet",
)

# Or a range:  "Review all changes between main and HEAD. ..."
```

## Follow Up

Use session resumption to ask the reviewer to clarify or elaborate.

```python
clarification = await reviewer.execute(
    cwd="/path/to/project",
    prompt="Explain the security concern in more detail and suggest a specific fix.",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],
    auto_approve=False,
    model="sonnet",
    session_id=review.session_id,
)
```

## Cross-Agent Review

Use a different agent or model than the one that wrote the code for genuine independence.
OpenCode ignores `allowed_tools`, so restrict it with `disallowed_tools` instead — that deny
*is* enforced. Don't swap `AgentType.CODEX` or `AgentType.CURSOR` into this example expecting
the same protection: they drop the deny with only a `UserWarning`.

```python
reviewer = AgentShell(agent_type=AgentType.OPENCODE)

review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review the uncommitted changes in this repository.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    disallowed_tools=["edit"],          # enforced: cannot Edit/Write/patch
    model="github-copilot/gpt-5.4",
)
```

> OpenCode reports `cost` as `0.0` for many models — use `output_tokens` if you need a usage
> figure. And a failed reviewer run raises `AgentExecutionError` rather than returning — wrap
> the call (see [Handling a Failed Review](#handling-a-failed-review) below), since OpenCode can
> truncate a review with no terminal event and no error (see the core skill's Error Handling).

## Prompt Patterns

### General Review
```
Review the uncommitted changes in this repository.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.
```

### Focused Security Review
```
Review the uncommitted changes for security vulnerabilities only. Check for:
- SQL injection, XSS, command injection
- Authentication/authorisation gaps
- Secrets or credentials in code
- Unsafe deserialization
```

### Requirements Validation
```
The requirement was: "{original_requirement}"

Review the uncommitted changes and assess whether they fully satisfy the requirement.
Identify any gaps, missing edge cases, or partial implementations.
```

### Test Coverage Review
```
Review the uncommitted changes and identify test scenarios that are missing.
For each gap, describe the test case that should exist and why it matters.
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Relying on a "do not modify files" prompt instruction for safety | Enforce it (whitelist + `auto_approve=False`, or `disallowed_tools`) |
| Whitelisting tools but leaving the default `auto_approve=True` | `--dangerously-skip-permissions` makes the whitelist inert — set `auto_approve=False` |
| Assuming `allowed_tools` restricts OpenCode, Codex, or Cursor | All three ignore it |
| `disallowed_tools` to keep a Codex/Cursor reviewer read-only | It only warns; sandbox instead |
| Believing `disallowed_tools=["edit"]` makes the reviewer read-only | It doesn't — the model writes via `bash`; also deny `bash` (and OS-sandbox for a hard guarantee) |
| Not giving the reviewer `Bash`/git access | Without it the reviewer can't run `git diff` — keep `bash`, or pass the diff in the prompt |
| Not catching `AgentExecutionError` | A failed reviewer run raises, it does not return |
| Reviewing with the same model that wrote the code | Use a different model or agent type for independence |
| Ignoring the review and committing anyway | At minimum, address all CRITICAL items first |
