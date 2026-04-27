# Qualification Proof Report

## Summary

- **Type**: `<Feature / Bug Verification>`
- **Name**: `<feature name or bug ID>`
- **Result**: <✅ QUALIFIED / ❌ NOT QUALIFIED / 🐛 BUG FIXED / 🐛 BUG NOT FIXED>
- **Date**: <ISO 8601 timestamp>
- **Test Plan**: `<relative path to test-plan.md>`

## Environment

| Component       | Version            |
| --------------- | ------------------ |
| OpenShift       |                    |
| MTV             |                    |
| CNV             |                    |
| Source Provider | `<type + version>` |
| Cluster API     |                    |

## Test Execution Results

| Test | Result | Duration | Notes |
| ---- | ------ | -------- | ----- |
|      |        |          |       |

### Test Output

<details><summary>Full pytest output</summary>

```text
<full pytest stdout/stderr>
```

</details>

## Cluster Verification

Independent verification performed after test execution.

| Check | Resource | Status | Evidence |
| ----- | -------- | ------ | -------- |
|       |          |        |          |

### Raw Evidence

<details><summary>Resource details</summary>

```yaml
<oc get output>
```

</details>

## Qualification Decision

### Criteria Met

- [ ] All tests passed (exit code 0)
- [ ] All cluster verifications passed
- [ ] Test scenarios match test plan expectations
- [ ] Evidence collected for all verification points
- [ ] Versions recorded

### Verdict

<QUALIFIED/NOT QUALIFIED with explanation>
