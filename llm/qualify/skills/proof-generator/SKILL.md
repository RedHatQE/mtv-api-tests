---
name: proof-generator
description: Assembles structured proof.md reports from test execution results and cluster verification evidence. Use when generating proof of test execution for the /qualify workflow.
---

# Proof Generator Skill

## Purpose

Takes test execution output, a cluster verification report, and version information
and produces a final `proof.md` document. This document serves as evidence that tests
passed AND cluster state confirms the expected outcome. The proof report is self-contained —
a human reading it must understand what was tested and what the evidence shows without
needing to re-run anything.

## Inputs Expected

Collect all of the following before generating the report:

| Input                           | Source                                                 | Required                                     |
| ------------------------------- | ------------------------------------------------------ | -------------------------------------------- |
| **Test execution output**       | pytest stdout/stderr, exit code, per-test results      | Yes                                          |
| **Cluster verification report** | Output from the `cluster-verifier` agent               | Yes                                          |
| **Version information**         | OCP version, MTV version, CNV version                  | Yes — report is NOT QUALIFIED if any missing |
| **Test plan reference**         | Path or link to the test-plan.md that was executed     | Yes                                          |
| **Type**                        | `feature` or `bug`                                     | Yes                                          |
| **Bug details** (bugs only)     | Bug ID/URL and whether the bug was FIXED or NOT FIXED  | Yes when type is `bug`                       |
| **Source provider info**        | Provider type and version (e.g., vSphere 8.0)          | Yes                                          |
| **Cluster API URL**             | API endpoint of the target cluster                     | Yes                                          |

## Proof Report Template

Generate `proof.md` using this exact structure. Replace every `<placeholder>` with real data.

````markdown
# Qualification Proof Report

## Summary
- **Type**: Feature / Bug Verification
- **Name**: <feature name or bug ID>
- **Result**: ✅ QUALIFIED / ❌ NOT QUALIFIED / 🐛 BUG FIXED / 🐛 BUG NOT FIXED
- **Date**: <ISO 8601 timestamp, e.g. 2026-04-27T14:32:00Z>
- **Test Plan**: [test-plan.md](<relative or absolute path to test-plan.md>)

## Environment
| Component | Version |
|-----------|---------|
| OpenShift | X.Y.Z |
| MTV | X.Y.Z |
| CNV | X.Y.Z |
| Source Provider | <type + version> |
| Cluster API | <URL> |

## Test Execution Results
| Test | Result | Duration | Notes |
|------|--------|----------|-------|
| test_create_storagemap | ✅ PASSED | 2.3s | |
| test_create_networkmap | ✅ PASSED | 1.8s | |
| test_create_plan | ✅ PASSED | 3.1s | |
| test_migrate_vms | ✅ PASSED | 120.5s | |
| test_check_vms | ✅ PASSED | 45.2s | |

### Test Output
<details><summary>Full pytest output</summary>

```
<full pytest stdout/stderr>
```

</details>

## Cluster Verification
Independent verification performed after test execution.

| Check | Resource | Status | Evidence |
|-------|----------|--------|----------|
| VM Exists | vm/<name> | ✅ PASS | |
| VM Running | vm/<name> | ✅ PASS | status.ready=true |
| Disks Bound | pvc/<name> | ✅ PASS | phase=Bound |
| Plan Succeeded | plan/<name> | ✅ PASS | status=Succeeded |
| Migration Completed | migration/<name> | ✅ PASS | |

### Raw Evidence
<details><summary>VM YAML</summary>

```yaml
<full VM resource YAML>
```

</details>

<details><summary>Plan Status</summary>

```yaml
<full Plan resource YAML or status section>
```

</details>

## Qualification Decision

### Criteria Met
- [x] All tests passed (exit code 0)
- [x] All cluster verifications passed
- [x] Test scenarios match test plan expectations
- [x] Evidence collected for all verification points

### Verdict
**✅ QUALIFIED** — All tests passed with cluster verification proof.
````

## Rules

### Qualification Logic

Applies to `type=feature`. For `type=bug`, use **Bug Verification Logic** verdict labels.

1. **NEVER** mark as `✅ QUALIFIED` if any test failed (exit code ≠ 0 or any individual test result is not PASSED).
2. **NEVER** mark as `✅ QUALIFIED` if cluster verification has any `❌ FAIL` check.
3. If both tests and cluster verification pass → `✅ QUALIFIED`.
4. If any test or verification fails → `❌ NOT QUALIFIED`. Include a clear **Reason** line under the verdict explaining which checks failed.

### Bug Verification Logic

- For `bug` type reports, the verdict is `🐛 BUG FIXED` or `🐛 BUG NOT FIXED` instead of QUALIFIED/NOT QUALIFIED.
- `🐛 BUG FIXED` — all tests pass, cluster verification confirms the fix, and the behavior described in the bug no longer reproduces.
- `🐛 BUG NOT FIXED` — tests fail or cluster verification shows the buggy behavior still present.
- Always include the bug ID/URL in the Summary section and reference the specific evidence that proves or disproves the fix.

### Evidence Requirements

- **Always** include raw evidence (YAML, logs) in collapsible `<details>` sections.
- **Always redact sensitive values** before writing proof artifacts
  (tokens, passwords, secrets, auth headers, private keys, kubeconfig credentials).
  Preserve only fields needed to validate behavior.
- **Versions are MANDATORY.** If any version (OCP, MTV, or CNV) is missing, mark the report as `❌ NOT QUALIFIED` with reason: `"Missing version information"`.
- The report must be **self-contained**. A reader must understand what was tested, what passed or failed, and what the cluster state looked like — all from the proof.md alone.

### Formatting

- Use `✅ PASSED` / `❌ FAILED` for individual test results.
- Use `✅ PASS` / `❌ FAIL` for cluster verification checks.
- Durations should be in seconds with one decimal place (e.g., `2.3s`).
- The Date field must be ISO 8601 format with timezone.
- Unchecked criteria boxes (`- [ ]`) must appear for any criteria not met, with an explanation.

### Handling Failures

When the verdict is NOT QUALIFIED or BUG NOT FIXED, add a `### Failure Details` section before the Verdict:

```markdown
### Failure Details
| Failed Item | Type | Details |
|-------------|------|---------|
| test_migrate_vms | Test | TimeoutError after 3600s |
| VM Running | Cluster Check | status.ready=false, phase=Scheduling |

### Verdict
**❌ NOT QUALIFIED** — 1 test failed, 1 cluster verification failed. See Failure Details above.
```

## Output Location

Write the generated proof report to:

- **Features**: `.qualify/features/<feature-name>/proof.md`
- **Bugs**: `.qualify/bugs/<bug-name-or-id>/proof.md`

The directory must match the directory used by the test plan. If the test plan lives at `.qualify/features/cold-migration/test-plan.md`, then the proof goes to `.qualify/features/cold-migration/proof.md`.
