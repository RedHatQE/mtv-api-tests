# Test Plan: <FEATURE/BUG NAME>

## Overview

**Type**: Feature / Bug Verification
**Source**: `<URL to design doc, Jira ticket, or bug report>`
**Date**: `<creation date>`
**Author**: AI-generated, human-reviewed

### Description

`<What is being tested and why. Written from a customer perspective.>`

## Prerequisites

### Cluster Requirements

- OpenShift version: `<minimum version if applicable>`
- MTV version: `<minimum version if applicable>`
- CNV installed: Yes/No

### Provider Requirements

- Source provider type: `<VMware/RHV/OpenStack/OVA/OCP>`
- Source provider version: `<if applicable>`
- Provider credentials: `<what's needed>`

### VM Requirements

| VM Name  | OS    | Power State | Guest Agent | Disk Type  | Special Config |
| -------- | ----- | ----------- | ----------- | ---------- | -------------- |
| `<name>` | `<os>`| on/off      | Yes/No      | thin/thick | `<notes>`      |

## Test Scenarios

### Scenario 1: <Descriptive Name — Customer Use Case>

**Description**: `<What the customer does and expects to happen>`

**Test Pattern**: 5-step / 6-step shared-disk / 6-step copy-offload

**Steps**:

1. Create StorageMap with `<specific mappings>`
2. Create NetworkMap with `<specific mappings>`
3. Create Plan with `<specific configuration>`
4. Execute migration
5. Verify migrated VMs
6. `<Only for 6-step patterns: shared-disk → verify shared disk data; copy-offload → verify XCOPY usage>`

**Expected Outcomes**:

- `<Specific expected result 1>`
- `<Specific expected result 2>`

**Cluster Verification Points**:

| What to Check      | How to Check                                                                             | Expected Value             |
| ------------------ | ---------------------------------------------------------------------------------------- | -------------------------- |
| VM exists, running | `oc get vm <name> -n <ns>`                                                               | status.ready=true          |
| Disks attached     | `oc get pvc -n <ns>`                                                                     | All PVCs Bound             |
| Network configured | `oc get vm <name> -n <ns> -o jsonpath='{.spec.template.spec.domain.devices.interfaces}'` | Correct network interfaces |
| `<additional>`     | `<command>`                                                                              | `<expected>`               |

### Scenario 2: `<Edge Case or Negative Scenario>`

...

## Test Configuration

### tests_params entry

```python
"test_<name>": {
    "virtual_machines": [
        {
            "name": "<vm-name>",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": False,
    "preserve_static_ips": False,
},
```

### Pytest Markers

- `@pytest.mark.<marker>` — `<reason>`

### Test File Location

`tests/<feature>/test_<feature>_migration.py`

## Risk Assessment

| Risk                 | Impact     | Mitigation         |
| -------------------- | ---------- | ------------------ |
| `<potential issue>`  | `<impact>` | `<how to handle>`  |
