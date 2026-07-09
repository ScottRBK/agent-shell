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
- **OpenCode / Codex** — they ignore `allowed_tools`; use the enforced denylist instead:
  `disallowed_tools=["edit"]`.

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
OpenCode ignores `allowed_tools`, so restrict it with `disallowed_tools` instead.

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
> figure. And `execute()` gives no failure signal: an empty `review.response` likely means the
> reviewer failed. If a reliable verdict matters, `stream()` and require a `result` event with
> `content == "ok"` — OpenCode can truncate a review with no terminal event and no error (see the
> core skill's Error Handling).

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
| Assuming `allowed_tools` restricts OpenCode | OpenCode ignores it — use `disallowed_tools` |
| Believing `disallowed_tools=["edit"]` makes the reviewer read-only | It doesn't — the model writes via `bash`; also deny `bash` (and OS-sandbox for a hard guarantee) |
| Not giving the reviewer `Bash`/git access | Without it the reviewer can't run `git diff` — keep `bash`, or pass the diff in the prompt |
| Not checking for empty responses from `execute()` | `execute()` gives no failure signal — an empty `review.response` likely means failure; use `stream()` to detect it |
| Reviewing with the same model that wrote the code | Use a different model or agent type for independence |
| Ignoring the review and committing anyway | At minimum, address all CRITICAL items first |
