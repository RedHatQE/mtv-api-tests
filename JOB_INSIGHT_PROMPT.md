# JOB_INSIGHT_PROMPT

## 1. Project Context

This is the test suite for **Migration Toolkit for Virtualization (MTV)** — a Red Hat product (upstream: Forklift)
that migrates virtual machines from VMware vSphere, Red Hat Virtualization (RHV), OpenStack, and OVA files
to OpenShift Virtualization (KubeVirt).

The tests validate the full migration lifecycle:

- Creating Provider, StorageMap, NetworkMap, and Plan custom resources (CRDs under `forklift.konveyor.io/v1beta1`)
- Executing cold, warm, and copy-offload migrations
- Post-migration VM validation (CPU, memory, disks, networks, IPs, guest agent, SSH connectivity)

Tests follow a 5-step incremental pattern per class:

1. `test_create_storagemap` — create StorageMap CR
2. `test_create_networkmap` — create NetworkMap CR
3. `test_create_plan` — create Plan CR
4. `test_migrate_vms` — execute migration and wait for completion
5. `test_check_vms` — validate migrated VMs

Tests use `@pytest.mark.incremental` — if an earlier step fails, subsequent steps in the same class are skipped.
When analyzing skipped tests, focus on the ROOT CAUSE (the first failure in the class).

**Source providers tested:** VMware vSphere (6.7-8.0+), RHV/oVirt, OpenStack, OVA, OpenShift (remote cluster migrations)
**Migration types:** Cold (VM off), Warm (VM on with precopy/cutover), Copy-Offload (XCOPY/VAAI)
**Test markers:** `tier0` (smoke), `warm`, `remote` (multi-cluster), `copyoffload`

---

## 2. Classification Rules

### CODE ISSUE — Test framework or test code problem

Indicators:

- Python import errors, syntax errors, or `AttributeError` in test code
- Fixture setup/teardown failures (e.g., `fixture_store` key errors, missing fixtures)
- Incorrect assertions or wrong expected values in test validation
- `TypeError` or `ValueError` from test utility functions (e.g., `utilities/mtv_migration.py`, `utilities/post_migration.py`)
- Test configuration errors (`py_config` key missing, wrong test parameters)
- `openshift-python-wrapper` API misuse (wrong resource class, missing parameters)
- SSH connection failures due to wrong credentials in test config
- Timeouts caused by incorrect wait conditions in test code (not product timeout)
- Errors in provider client code (`libs/base_provider.py`, `libs/providers/*`)

### PRODUCT BUG — Actual MTV/Forklift product defect

Indicators:

- Migration CR status shows `Failed` with error in pipeline steps (PreHook, DiskTransfer, PostHook)
- `forklift-controller` pod logs show errors or crashes
- VM migration stuck in `Running` state past reasonable timeout
- Post-migration VM validation failures: wrong CPU/memory count, missing disks, broken networking, lost static IPs
- Provider inventory API returning incorrect or missing data
- StorageMap or NetworkMap CR stuck in non-Ready state
- Plan CR validation webhook rejecting valid configurations
- VDDK (VMware) transfer errors, disk conversion failures
- Warm migration precopy/cutover failures
- Copy-offload XCOPY operation failures
- Guest agent not responding after migration (when it was working before)
- OpenShift resource creation succeeding but the resulting VM is misconfigured
- Errors from `forklift.konveyor.io` CRD controllers

### Custom Exception Signals

Key exceptions from `exceptions/exceptions.py` provide strong classification signals:

- `MigrationPlanExecError` — Migration failed or timed out; check if `plan_wait_timeout` is too low (CODE ISSUE) vs migration controller stalled (PRODUCT BUG)
- `ForkliftPodsNotRunningError` — Forklift pods crashed or not running → PRODUCT BUG / infrastructure
- `MigrationNotFoundError`, `MigrationStatusError`, `VmPipelineError` — Migration CR in bad state → usually PRODUCT BUG
- `VmCloneError`, `VmMissingVmxError`, `VmBadDatastoreError` — Source provider issues → investigate further (could be either)
- `MissingProvidersFileError` — Test configuration problem → CODE ISSUE
- `TimeoutExpiredError` (from `timeout_sampler`) — Common in traces; check if the timeout value is appropriate
  for the operation (CODE ISSUE) vs operation genuinely timed out (PRODUCT BUG)

### Ambiguous Cases

When uncertain:

- Check if the error originates from TEST CODE (`tests/`, `utilities/`, `libs/`, `conftest.py`) → likely CODE ISSUE
- Check if the error originates from CLUSTER/PRODUCT components (forklift-controller, kubevirt, CDI) → likely PRODUCT BUG
- Infrastructure failures (cluster unreachable, OCP API timeout, node NotReady) → classify as PRODUCT BUG with a note that it may be infrastructure-related

---

## 3. Analysis Thoroughness

**CRITICAL: Never dismiss or skip warnings, conditions, or errors found in the data.**
Every warning, condition entry, and error message in Plan/Migration/Provider CR status
MUST be considered as a potential contributing factor to the failure.

When you encounter warnings or conditions (e.g., Plan `status.conditions` with `category: Warn` or `Critical`):

- **Analyze whether the warning is related to the failure** — do not assume it is unrelated without explanation
- **If a warning could contribute to the failure**, include it in your root cause analysis
- **If a warning is genuinely unrelated**, briefly explain WHY it is unrelated to the specific failure mode
- **Multiple issues can coexist** — a failure may have a primary cause AND secondary issues flagged by warnings

Example: A Plan condition warning about unsupported IP preservation with Pod Networking may seem unrelated
to a ConvertGuest failure, but it could indicate a misconfigured Plan that affects downstream pipeline steps.
Investigate before dismissing.

---

## 4. Missing Information Guidance

**CRITICAL: For EVERY analysis (both CODE ISSUE and PRODUCT BUG), if the provided error, stack trace,
or console output lacks sufficient detail to make a confident diagnosis, you MUST include
a "missing_information" section listing what additional data would help.**

### For CODE ISSUE — suggest collecting

- Full fixture chain output (which fixture failed and why)
- Test configuration dump (`py_config` contents for the failing test)
- Provider connection details (is the source provider reachable?)
- The specific test parameter values used (from `@pytest.mark.parametrize`)
- Related utility function source code if not already provided

### For PRODUCT BUG — suggest collecting

- `forklift-controller` pod logs: `oc logs -n openshift-mtv deployment/forklift-controller`
- Migration CR status: `oc get migration <name> -n <namespace> -o yaml`
- Plan CR status: `oc get plan <name> -n <namespace> -o yaml`
- Provider CR status: `oc get provider <name> -n <namespace> -o yaml`
- VM pipeline details from Migration status (`status.vms[].pipeline[]`)
- Must-gather data: `oc adm must-gather --image=quay.io/kubev2v/forklift-must-gather:latest`
- Target VM status: `oc get vm <name> -n <namespace> -o yaml`
- CDI DataVolume status (for disk transfer issues)
- Source provider logs (vCenter events, RHV engine logs, OpenStack nova logs)
- Network attachment definitions (for network mapping issues)
- Storage class configuration and PVC status
- Events in the target namespace: `oc get events -n <namespace> --sort-by='.lastTimestamp'`

### For Infrastructure Issues

- Cluster node status: `oc get nodes`
- Cluster operators status: `oc get co`
- OpenShift version: `oc version`
- MTV operator version: `oc get csv -n openshift-mtv`

---

## 5. Key Product Components

| Component | Purpose | Where to look for issues |
| --- | --- | --- |
| `forklift-controller` | Orchestrates migrations | Pod logs in `openshift-mtv` namespace |
| `forklift-inventory` | REST API for source provider data | Route in `openshift-mtv`, inventory queries |
| `virt-v2v` | VM disk conversion | Migration pod logs in target namespace |
| `CDI` (Containerized Data Importer) | Disk import/upload | DataVolume and CDI pod logs |
| `KubeVirt` | VM runtime on OpenShift | VirtualMachine and VMI status |
| Provider CRDs | Source platform connections | Provider CR status and conditions |
| StorageMap/NetworkMap | Resource mapping definitions | Map CR status (Ready/NotReady) |
| Plan CR | Migration workflow definition | Plan CR status and conditions |
| Migration CR | Execution tracking | Migration status, VM pipeline steps |
| Hook CR | Pre/post migration automation | Hook CR status, hook pod logs in target namespace |

---

## 6. Common Failure Patterns

### Migration Timeout

- **If test code has wrong timeout value** → CODE ISSUE
- **If migration genuinely stalls** (DiskTransfer stuck, controller not progressing) → PRODUCT BUG
- Check: Does the timeout match the expected migration duration for the VM size?

### Post-Migration Validation Failures (`test_check_vms`)

- **Wrong expected values in test** (e.g., expects 4 CPUs but VM has 2) → CODE ISSUE
- **VM actually misconfigured after migration** (lost disks, wrong network) → PRODUCT BUG
- Check: Compare source VM spec with destination VM spec

### Provider Connection Failures

- **Wrong credentials/URL in test config** → CODE ISSUE
- **Provider actually unreachable/API broken** → PRODUCT BUG (or infrastructure)

### Warm Migration Failures

- Precopy snapshot failures → usually PRODUCT BUG (forklift-controller issue)
- Cutover timing issues → check if `mins_before_cutover` or `snapshots_interval` / `controller_precopy_interval` are misconfigured (CODE ISSUE) vs controller failure (PRODUCT BUG)

### Copy-Offload (XCOPY) Failures

- Storage vendor compatibility issues → PRODUCT BUG
- Wrong storage vendor configuration in test → CODE ISSUE
- XCOPY operation timeout or array-level errors → PRODUCT BUG

### Resource Creation Failures (Steps 1-3)

- `create_and_store_resource()` failures → check if parameters are wrong (CODE ISSUE) or if the CRD controller rejects valid input (PRODUCT BUG)
- Name conflicts (resource already exists) → usually CODE ISSUE (test isolation problem)

### SSH Validation Failures (`test_check_vms`)

- SSH managed by `SSHConnectionManager` and `vm_ssh_connections` fixture
- **Wrong SSH credentials or key in test config** → CODE ISSUE
- **VM network not properly configured after migration** (no route to host, connection refused) → PRODUCT BUG
- Check: Is the VM reachable? Is the guest agent running? Are network mappings correct?

### Timeout Polling Failures

- Uses `TimeoutSampler` from `timeout_sampler` package for polling operations
- `TimeoutExpiredError` appears frequently in traces
- **Timeout value too low for the operation** → CODE ISSUE (adjust `plan_wait_timeout` or sampler timeout)
- **Operation genuinely never completed** (migration stuck, resource never became Ready) → PRODUCT BUG

### Resource Leftovers

- Stale resources from previous test runs (namespaces, VMs, Plans, Maps) can cause name conflicts or unexpected state
- **Test isolation failure** (missing cleanup, `create_and_store_resource()` not used) → CODE ISSUE
- **Product failed to delete resources** (finalizer stuck, controller not cleaning up) → PRODUCT BUG
- Check: Are there leftover resources in the target namespace from a previous run? Did the `cleanup_migrated_vms` fixture run properly?

---

## 7. Technology Stack

- **Test framework:** pytest with `pytest-testconfig`, `@pytest.mark.incremental`, optionally `pytest-xdist` for parallel execution
- **OpenShift interactions:** `openshift-python-wrapper` (`ocp_resources.*`, `ocp_utilities.*`) — NEVER direct `kubernetes` client
- **Provider SDKs:** pyVmomi (VMware), ovirtsdk4 (RHV), openstacksdk (OpenStack)
- **SSH validation:** paramiko
- **Resource management:** All resources created via `create_and_store_resource()` utility

---

## 8. Reference Links

### Product Documentation

- [MTV 2.10 Documentation](https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.10)
- [MTV CLI Migration with CRDs](https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.10/html/planning_your_migration_to_red_hat_openshift_virtualization/assembly_migrating-vms-cli_mtv)
- [OpenShift Virtualization 4.21 Documentation](https://docs.redhat.com/en/documentation/red_hat_openshift_virtualization/4.21)

### Upstream Repositories

- [kubev2v/forklift](https://github.com/kubev2v/forklift) — Main Forklift monorepo (operator, controller, validation)
- [kubev2v/forklift-console-plugin](https://github.com/kubev2v/forklift-console-plugin) — OpenShift Console UI plugin
- [kubev2v/forklift-must-gather](https://github.com/kubev2v/forklift-must-gather) — Diagnostic data collector
- [kubev2v/forklift-documentation](https://github.com/kubev2v/forklift-documentation) — Upstream docs
- [Copy-Offload (XCOPY) Volume Populator](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator)

### Test Infrastructure

- [RedHatQE/openshift-python-wrapper](https://github.com/RedHatQE/openshift-python-wrapper) — OpenShift Python wrapper library
- [openshift-python-wrapper API docs](https://openshift-python-wrapper.readthedocs.io/en/latest/)

### CRD References

- [Forklift CRDs source](https://github.com/kubev2v/forklift/tree/main/operator/config/crd/bases) — NetworkMap, StorageMap, Provider, Plan, Migration schemas
- [Hooks documentation](https://github.com/kubev2v/forklift/blob/main/docs/hooks.md) — PreHook/PostHook specification
