---
description: "Full qualification workflow: test plan → write tests → verify on cluster → PR with proof"
argument-hint: "<--type feature|bug> <--source URL|file> <--cluster kubeconfig-path> [--name identifier]"
---

# /qualify — Full Qualification Workflow

## Arguments

```text
$ARGUMENTS
```

## Overview

This prompt orchestrates a full qualification workflow: from feature design or bug report to a verified PR with proof.
It overrides the normal "AI must NEVER run tests" rule — tests ARE executed on a real cluster during this workflow.

## Phase 0: Parse Arguments & Setup

1. **Parse arguments** from the raw text above:
   - `--type`: `feature` or `bug` (REQUIRED)
   - `--source`: URL (Jira, GitHub issue, design doc) or local file path (REQUIRED)
   - `--cluster`: Path to kubeconfig file (REQUIRED). Do not use implicit current context.
   - `--name`: Short identifier for this qualification (e.g., `warm-migration-rhv`, `JIRA-12345`). If not provided, derive from source.

- Normalize `name` to a safe slug before any use:
  - allowed chars: `a-z`, `0-9`, `-`
  - replace all other chars with `-`
  - collapse repeated `-`, trim leading/trailing `-`
  - max length 63
  - reject values containing `..`, `/`, `\`

   If required arguments are missing, ask the user to provide them (use `ask_user` if available, otherwise ask in chat).

1. **Validate cluster connectivity**:

   ```bash
   # If --cluster provided:
   export KUBECONFIG=<path>

   # Validate:
   oc whoami
   oc cluster-info
   ```

   If cluster is unreachable, STOP and ask the user to fix it.

2. **Collect environment versions** (save for proof.md):

   ```bash
   # OCP version
   OCP_VERSION_RAW="$(oc get clusterversion version -o jsonpath='{.status.desired.version}' 2>&1)"
   OCP_VERSION="$(printf '%s\n' "$OCP_VERSION_RAW")"
   # if empty or command error -> record: UNKNOWN: <OCP_VERSION_RAW or "no clusterversion match">

   # MTV version (from CSV)
   MTV_VERSION_RAW="$(oc get csv -n openshift-mtv -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.version}{"\n"}{end}' 2>&1)"
   MTV_VERSION="$(printf '%s\n' "$MTV_VERSION_RAW" | grep mtv || true)"
   # if empty -> record: UNKNOWN: <MTV_VERSION_RAW or "no CSV match">

   # CNV version (from CSV)
   CNV_VERSION_RAW="$(oc get csv -n openshift-cnv -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.version}{"\n"}{end}' 2>&1)"
   CNV_VERSION="$(printf '%s\n' "$CNV_VERSION_RAW" | grep kubevirt || true)"
   # if empty -> record: UNKNOWN: <CNV_VERSION_RAW or "no CSV match">
   ```

   If any version cannot be retrieved, record it as `UNKNOWN` with the error message and continue the workflow.
   **Note:** `UNKNOWN` versions will force the final verdict to `❌ NOT QUALIFIED` or `🐛 BUG NOT FIXED`
   per proof-generator rules (versions are mandatory). The workflow still proceeds to collect all other evidence.

3. **Create output directory** and define `artifact_key` for all later paths:
   - Feature: `.qualify/features/<name>/` — `artifact_key` = `<name>`
   - Bug: `.qualify/bugs/<id>/` — `artifact_key` = `<id>`

4. **For bugs only — extract bug ID**:
   - Extract bug ID from `--source` URL (e.g., Jira ticket key from `https://issues.redhat.com/browse/MTV-1234`, GitHub issue number from `https://github.com/org/repo/issues/42`)
   - If `--name` was provided, use it as the bug ID (user override)
   - If extraction fails and `--name` was not provided, ask the user: "Could not extract bug ID from source. Please provide bug ID using --name (e.g., `MTV-1234`, `BZ-67890`, `42`)"
   - Normalize the extracted/provided bug ID using the same slug rules from step 1 (lowercase, safe chars, max 63)
   - Use the normalized bug ID as `<id>` for directory creation in step 3

5. **For bugs only** — ask the user:
   > "Should this bug get a permanent test in the test suite? (Yes = full PR flow, No = verify-only with proof.md)"

## Phase 1: Test Plan

1. **Fetch source material**: Read the feature doc or bug report from the --source URL/file.
   Use `fetch_content` for URLs, `read` for local files.

2. **Delegate to test-planner agent** (from `llm/qualify/agents/test-planner.md`):
   Tell the agent:
   - The source material content
   - The type (feature or bug)
   - To read `AGENTS.md` for project standards (includes coding patterns, test structure, and the `/qualify` exception to the test execution prohibition)
   - To read existing tests in `tests/` for examples
   - To read `llm/qualify/templates/test-plan-template.md` for the output template
   - To produce `test-plan.md`

3. **Save** the test plan to `.qualify/<type>/<artifact_key>/test-plan.md`

4. **🛑 HUMAN CHECKPOINT**: Ask the user:
   > "Test plan ready for review. Please review `.qualify/<type>/<artifact_key>/test-plan.md`.
   > Approve or provide feedback?"

   Options: ["Approved — proceed to implementation", "I have feedback"]

   If feedback: update test plan and re-ask. Loop until approved.

## Phase 2: Write & Verify Tests

This phase is **fully autonomous** — no human intervention unless the AI gets stuck.

### For features and bugs-with-permanent-tests

1. **Create git branch**: Delegate to git-expert:

   ```bash
   git fetch origin main
   git checkout -b qualify/<name> origin/main
   ```

   Note: Qualification branches intentionally use the `qualify/` prefix to distinguish them from regular `feat/` and `fix/` branches.

2. **Write tests**: Delegate to python-expert:
   - Provide the approved test plan
   - Provide `AGENTS.md` for coding standards (all project constraints are in this file)
   - Tell it to follow the 5/6-step test pattern
   - Tell it to create the test config in `tests/tests_config/config.py`
   - Tell it to create the test file in the appropriate `tests/<feature>/` directory
   - Tell it to create any needed fixtures in the appropriate `conftest.py`

3. **Run tests on cluster**:

   ```bash
   # Set KUBECONFIG if provided
   export KUBECONFIG=<path>

   # Run the specific test (pipefail preserves pytest exit code through tee)
   set -o pipefail
   uv run pytest tests/<path>::<TestClass> -v --tc-file=tests/tests_config/config.py --tc-format=python -p no:xdist 2>&1 | tee .qualify/<type>/<artifact_key>/test-output.log
   PYTEST_EXIT=${PIPESTATUS[0]}
   ```

   Capture the full output.

4. **Verify on cluster**: Delegate to cluster-verifier agent (from `llm/qualify/agents/cluster-verifier.md`):
   - Provide the test plan (what to verify)
   - Provide the namespace used by the test
   - The agent checks cluster state independently

5. **Evaluate results**:
   - Tests passed AND cluster verification passed → proceed to Phase 3
   - Tests failed → delegate to python-expert to fix, then re-run (go to step 3)
   - Cluster verification failed (tests said pass but cluster state wrong) → investigate and fix
   - **AI stuck** → ask the user: "I'm stuck on: `<specific problem>`. How should I proceed?"

6. **Loop with bounded retries**:
   - Track `attempt_count` for the current failing error signature.
   - If the same failure persists for 3 attempts, STOP autonomous retries and ask the user for guidance.
   - Resume only after user guidance; reset counter when error signature changes.

### For bugs-verify-only (no permanent test)

1. Write a **temporary test file** in `/tmp/qualify-<name>/` (not in the repo)
2. Run it on the cluster using the temporary path, for example:

   ```bash
   export KUBECONFIG=<path>
   set -o pipefail
   uv run pytest /tmp/qualify-<name>/<test_file>.py -v \
     --tc-file=tests/tests_config/config.py --tc-format=python -p no:xdist \
     2>&1 | tee .qualify/<type>/<artifact_key>/test-output.log
   PYTEST_EXIT=${PIPESTATUS[0]}
   ```

3. Verify on cluster (same as step 4 above)
4. Skip Phase 3 (no PR needed), go directly to Phase 4

## Phase 3: Code Review & PR

Only for features and bugs-with-permanent-tests.

1. **Internal code review**:

   **pi (myk-org/pi-config):** Run these 3 reviewers IN PARALLEL:
   - `code-reviewer-quality`
   - `code-reviewer-guidelines`
   - `code-reviewer-security`

   **Other environments:** Follow `AGENTS.md` / `CLAUDE.md` and delegate to `code-reviewer` after each change.

   Fix any issues. Re-review until no findings remain.

2. **Pre-commit**: Run `pre-commit run --all-files`. Fix any failures.

3. **Create PR**: Delegate to github-expert:
   - Title: `[qualify] <type>: <name>`
   - Body includes:
     - Link to source (Jira/design doc)
     - Summary of what was tested
     - Link to proof.md location
     - Qualification verdict
   - Add label: `qualified` (if label exists)

## Phase 4: Generate Proof

1. **Assemble proof**: Read the skill instructions from `llm/qualify/skills/proof-generator/SKILL.md`.
   Follow its instructions to produce proof.md using:
   - Test execution output (from Phase 2)
   - Cluster verification report (from Phase 2)
   - Version information (from Phase 0)
   - Test plan reference
   - The template from `llm/qualify/templates/proof-template.md`

2. **Write proof.md** to `.qualify/<type>/<artifact_key>/proof.md`

3. **Final summary** to the user:

   ```text
   ## Qualification Complete

   Type: feature/bug
   Name: <name>
   Result: QUALIFIED / NOT QUALIFIED / BUG FIXED / BUG NOT FIXED

   Artifacts:
   - Test Plan: .qualify/<type>/<artifact_key>/test-plan.md
   - Proof: .qualify/<type>/<artifact_key>/proof.md
   - PR: <URL> (if applicable)

   Environment:
   - OCP: X.Y.Z
   - MTV: X.Y.Z
   - CNV: X.Y.Z
   ```

## Critical Rules

1. **Never mark as QUALIFIED without cluster verification proof** — test passing alone is NOT sufficient
2. **Never skip Phase 1 human review** — the test plan MUST be approved before writing code
3. **Always collect versions in Phase 0** — if versions cannot be determined, report and ask user
4. **This prompt overrides the "never run tests" rule** — running pytest on a real cluster is required
5. **When stuck, ask the user** — do not loop indefinitely. If after 3 attempts a test still fails with the same error, ask for guidance
6. **All proof must be self-contained** — proof.md must be readable without needing to re-run anything
7. **Bug verify-only mode skips PR** — only produces proof.md
