---
name: delegating-code-review
description: Use when you have made code changes and want another CLI agent to review them before committing or continuing. Covers crafting review prompts, scoping reviewer permissions to read-only, interpreting feedback, and follow-up clarification sessions.
---

# Delegating Code Review to Another Agent

Use AgentShell to invoke another CLI agent to review changes in a repository. The reviewing agent has its own tools — just point it at the repo, tell it what to look at, and let it do the work.

Assumes familiarity with AgentShell basics. See [invoking-cli-agents](../invoking-cli-agents/SKILL.md) for setup and core API.

## When to Use

- You have made code changes and want a second opinion before committing
- You want to validate that changes meet requirements before marking work complete
- You need a security, performance, or correctness review
- You want to check for regressions or unintended side effects

## Review Uncommitted Changes

Tell the reviewer to look at the current uncommitted changes in the working directory.

```python
from agent_shell.shell import AgentShell
from agent_shell.models.agent import AgentType

reviewer = AgentShell(agent_type=AgentType.CLAUDE_CODE)

review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review the uncommitted changes in this repository.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],
    model="sonnet",
)
```

The reviewer will run `git diff` itself, read surrounding code for context, and report findings.

## Review a Specific Commit or Range

Point the reviewer at a particular changeset.

```python
review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review the changes in commit abc1234.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],
    model="sonnet",
)
```

```python
# Review a range of commits
review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review all changes between main and HEAD.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],
    model="sonnet",
)
```

## Follow Up

Use session resumption to ask the reviewer to clarify or elaborate.

```python
clarification = await reviewer.execute(
    cwd="/path/to/project",
    prompt="Can you explain the security concern in more detail and suggest a specific fix?",
    allowed_tools=["Read", "Glob", "Grep", "Bash"],
    model="sonnet",
    session_id=review.session_id,
)
```

## Cross-Agent Review

Use a different agent or model than the one that wrote the code for genuine independence.

> **Safety note:** Only Claude Code respects `allowed_tools`. OpenCode ignores it — the agent has access to all tools regardless. When using OpenCode as a reviewer, instruct it not to modify files in the prompt.

```python
# Review with OpenCode using a different model
reviewer = AgentShell(agent_type=AgentType.OPENCODE)

review = await reviewer.execute(
    cwd="/path/to/project",
    prompt="""Review the uncommitted changes in this repository. DO NOT modify any files.
Focus on correctness, security, and design.
Flag issues by severity: CRITICAL, WARNING, or SUGGESTION.""",
    model="github-copilot/gpt-5.4",
)
```

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
| Manually capturing diffs and passing them in the prompt | Let the reviewer run `git diff` itself — it has tools |
| Using OpenCode and assuming `allowed_tools` works | OpenCode ignores tool restrictions — use prompt instructions or use Claude Code |
| Reviewing with the same model that wrote the code | Use a different model or agent type for independence |
| Ignoring the review and committing anyway | At minimum, address all CRITICAL items before proceeding |
| Not giving the reviewer `Bash` access | Without `Bash`, the reviewer can't run `git diff` or `git log` to inspect changes |
