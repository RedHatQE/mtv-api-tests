# /qualify — AI Qualification Workflow

Full qualification workflow for MTV API tests: from feature design or bug report to verified PR with proof.

## What It Does

```bash
/qualify --type feature --source <url> --cluster ~/kubeconfig
```

1. **Test Plan** — AI reads feature/bug docs → produces a test plan → human reviews
2. **Write Tests** — AI writes E2E customer use-case tests following project patterns
3. **Verify on Cluster** — AI runs tests on a real cluster AND independently verifies cluster state
4. **Code Review** — AI reviewers check the code (on pi with myk-org/pi-config: 3 parallel reviewers; elsewhere per project `AGENTS.md` / `CLAUDE.md`)
5. **PR with Proof** — Creates PR with proof.md documenting test results + cluster evidence + versions

### Outputs

| Artifact        | Location                                                                           |
| --------------- | ---------------------------------------------------------------------------------- |
| Test plan       | `.qualify/features/<name>/test-plan.md` or `.qualify/bugs/<id>/test-plan.md`       |
| Proof report    | `.qualify/features/<name>/proof.md` or `.qualify/bugs/<id>/proof.md`               |
| Test output log | `.qualify/features/<name>/test-output.log` or `.qualify/bugs/<id>/test-output.log` |
| PR              | GitHub (features and bugs with permanent tests)                                    |

## Arguments

| Argument    | Required | Description                                                            |
| ----------- | -------- | ---------------------------------------------------------------------- |
| `--type`    | Yes      | `feature` or `bug`                                                     |
| `--source`  | Yes      | URL to Jira ticket, GitHub issue, design doc, or local file path       |
| `--cluster` | No       | Path to kubeconfig. If omitted, uses current `oc` context              |
| `--name`    | No       | Short identifier (e.g., `warm-migration-rhv`). Auto-derived if omitted |

## Usage Examples

### Qualify a New Feature

```bash
/qualify --type feature --source https://issues.redhat.com/browse/MTV-1234 --cluster ~/kubeconfigs/test-cluster
```

### Verify a Bug Fix

```bash
/qualify --type bug --source https://issues.redhat.com/browse/MTV-5678 --name MTV-5678
```

The AI will ask: "Should this bug get a permanent test in the test suite?"

- **Yes** → full flow: test plan → write test → PR → proof
- **No** → verify-only: test plan → run throwaway test → proof.md (no PR)

## Human Checkpoints

The workflow is fully automated EXCEPT at these points:

| Checkpoint         | When                        | What                                      |
| ------------------ | --------------------------- | ----------------------------------------- |
| Test plan review   | After Phase 1               | Approve or give feedback on the test plan |
| Bug: suite or not? | Start of bug workflow       | Decide if test joins permanent suite      |
| AI stuck           | When AI can't make progress | Guide the AI on how to proceed            |
| PR review          | After Phase 3               | Normal GitHub PR review                   |

## Setup by AI CLI

### pi

1. Add to `.pi/settings.json`:

   ```json
   {
     "prompts": ["llm/qualify/prompts"],
     "skills": ["llm/qualify/skills"]
   }
   ```

2. Register agents — add to your pi-config or project agents:

   ```json
   {
     "agents": ["llm/qualify/agents"]
   }
   ```

3. Use: type `/qualify` in pi's interactive mode.

### Claude Code

1. Copy or symlink the prompt template:

   ```bash
   mkdir -p .claude/commands
   cp llm/qualify/prompts/qualify.md .claude/commands/qualify.md
   ```

2. Reference agents and skills in `CLAUDE.md`:

   ```markdown
   ## Qualification Workflow
   See `llm/qualify/` for the /qualify workflow:
   - Agents: `llm/qualify/agents/`
   - Skills: `llm/qualify/skills/`
   - Templates: `llm/qualify/templates/`
   ```

3. Use: type `/qualify` in Claude Code.

### Cursor

1. Add as a Notepad or Rule:
   - Copy content from `llm/qualify/prompts/qualify.md` into a Cursor Rule
   - Reference agent/skill files in the rule

2. Or use `.cursorrules` to reference the qualify workflow.

### Other AI CLIs

The workflow is plain Markdown — adapt to any AI CLI that supports:

- Prompt templates or system prompts
- Agent/persona definitions
- Tool access (file read/write, bash execution, web fetching)

Copy the relevant `.md` files into your CLI's configuration format.

## Directory Structure

```text
llm/qualify/
├── README.md                         # This file
├── prompts/
│   └── qualify.md                    # Main prompt template (/qualify command)
├── agents/
│   ├── test-planner.md               # Reads docs → produces test plans
│   └── cluster-verifier.md           # Independently verifies cluster state
├── skills/
│   └── proof-generator/
│       └── SKILL.md                  # Assembles proof.md reports
└── templates/
    ├── test-plan-template.md         # Test plan skeleton
    └── proof-template.md             # Proof report skeleton
```

Output (gitignored):

```text
.qualify/
├── features/
│   └── <name>/
│       ├── test-plan.md
│       ├── test-output.log
│       └── proof.md
└── bugs/
    └── <name>/
        ├── test-plan.md
        ├── test-output.log
        └── proof.md
```

## Requirements

- `oc` CLI configured and authenticated to a working OpenShift cluster
- MTV operator installed on the cluster
- CNV installed on the cluster
- Source provider configured (VMware, RHV, etc.) with test VMs available
- `.providers.json` configured in the repo
