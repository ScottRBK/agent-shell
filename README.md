# Agent Shell
Agent Shell is a light weight abstraction for executing a cli coding agent headlessly
and returning the output that can be used programatically as a unified contract


## Example 

```python
from agent_shell import AgentShell

agent_shell = AgentShell(agent_type="claude_code")

response = agent_shell.execute(
        cwd="/~/dev/agentshell",
        prompt="Can you tell me about this project?",
        allowed_tools="Read,Glob,Grep",
        model="sonnet"
)

print("Agents Response:\n\n")
print(response)
```

## Supported CLI Agents:
[] Claude Code 
[] OpenCode
[] Gemini CLI
[] Copilot CLI
[] Codex



