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

## MANDATORY: Use screenshots / images from build artifacts

When `build-artifacts/` is available, you MUST look for Playwright failure media and use it:

1. Prefer failure-scoped paths under `build-artifacts/test-results/`:
   - `**/test-failed-*.png` — screenshots of the failure
   - sibling `error-context.md` — text context for the same failure
2. For each relevant screenshot, use the `read` tool (vision) and include what you see in `artifacts_evidence`, e.g.:
   - `[build-artifacts/test-results/.../test-failed-1.png]: Migration nav item missing; page stuck on Contents progress bar`
3. Also read sibling text artifacts (`error-context.md`, console/logs) and cite them as usual.
4. Videos (`*.webm`) may be present but cannot be played via `read` — do not pretend to watch them; use screenshots and `error-context.md` instead.

Do not classify from the stack trace alone when matching screenshots exist.
