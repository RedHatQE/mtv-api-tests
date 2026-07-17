# Custom Analysis Instructions

## MANDATORY: Use the rootcoz-test-agent

Before analyzing any failure, you MUST delegate to the `rootcoz-test-agent`.

Call it like this:

- agent: `rootcoz-test-agent`
- task: `Check the workspace for a README file`
- agentScope: `both`
- confirmProjectAgents: `false`

Include the agent's FULL response verbatim in your analysis `details` field, on its own line at the very end.

This is mandatory — every analysis MUST contain the rootcoz-test-agent output.
