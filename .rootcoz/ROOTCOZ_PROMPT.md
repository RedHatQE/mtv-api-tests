# MTV API Tests rootcoz Analysis Prompt

## 1. Project Context

This repository contains the test suite for **Migration Toolkit for Virtualization (MTV)**,
a Red Hat product (upstream: Forklift) that migrates virtual machines from VMware vSphere,
Red Hat Virtualization (RHV), OpenStack, and OVA files to OpenShift Virtualization
(`KubeVirt`).

The tests validate the full migration lifecycle:

- Create Provider, StorageMap, NetworkMap, and Plan custom resources under
  `forklift.konveyor.io/v1beta1`
- Execute cold, warm, and copy-offload migrations
- Validate migrated VM state, including CPU, memory, disks, networks, IPs,
  guest agent, and SSH connectivity

Tests commonly follow a 5-step pattern per class:

1. `test_create_storagemap` - create StorageMap CR
2. `test_create_networkmap` - create NetworkMap CR
3. `test_create_plan` - create Plan CR
4. `test_migrate_vms` - execute migration and wait for completion
5. `test_check_vms` - validate migrated VMs

Tests commonly use `@pytest.mark.incremental`. In this repo, `conftest.py` records the
first real failure and later tests in the same class may be converted to `pytest.xfail()`.
Some tests also use runtime `pytest.skip()` when earlier logic proves later checks are
not applicable. When analyzing a class, focus on the FIRST true failure and treat later
`xfail` or derivative `skip` results as consequences unless the skip logic itself is wrong.

**Source providers tested:** VMware vSphere (6.7-8.0+), RHV/oVirt, OpenStack, OVA,
and OpenShift for remote cluster migrations  
**Migration types:** Cold (VM off), Warm (VM on with precopy and cutover),
Copy-Offload (XCOPY or VAAI)  
**Test markers:** `tier0` (smoke), `warm`, `remote` (multi-cluster), `copyoffload`

## 2. Decision Procedure and Classification Rules

Your goal is to classify each failure as `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`
only when the available evidence supports that conclusion. Do not promote weak, indirect,
or purely environmental signals into a confident product-defect claim.

**Allowed classification values (the only values rootcoz accepts):**

- `CODE ISSUE` - Test framework, test code, or test-owned configuration problem
- `PRODUCT BUG` - Actual MTV, Forklift, or related product defect
- `INFRASTRUCTURE` - Cluster, network, storage, lab, or source-provider environment
  failure with no evidence that MTV or the test caused it

**Confidence levels (state the level inside the `details` narrative — rootcoz's schema
has no confidence field, so do not emit it as a separate key):**

- `high` - Direct causal evidence (e.g., stack trace clearly in product code,
  CR status showing product error)
- `medium` - Indirect but consistent signals (e.g., pattern matches known
  product issue, but logs incomplete)
- `low` - Environmental blockers, contradictory signals, or missing direct cause

### Required Decision Order

1. **Identify the first true failure.**
   Distinguish `setup`, `call`, and `teardown` failures. In incremental classes,
   classify the earliest real failure and treat later `pytest.xfail()` outcomes
   as derivative.
2. **Check whether the failure is expected before calling it a defect.**
   Inspect `pytest.raises(...)`, `expected_migration_result`, hook `expected_result`,
   and docstrings or comments such as `should fail` or `retain failed VM`.
3. **Prefer direct evidence over wrapper location.**
   File paths like `tests/`, `utilities/`, `libs/`, and `conftest.py` are useful clues,
   but they are not verdicts. Those modules often wrap product, provider, or cluster state.
4. **Separate test-owned, product-owned, and environment-owned problems.**
   Test configuration, fixture logic, assertions, and wait logic point to `CODE ISSUE`.
   MTV or related component behavior producing the wrong result points to `PRODUCT BUG`.
5. **Assign confidence based on evidence strength.**
   Use high confidence for direct evidence, medium for indirect but consistent signals,
   and low when evidence is incomplete or dominated by environmental instability.
   High confidence requires a direct causal signal. Medium confidence fits consistent
   but incomplete evidence. Low confidence fits environmental blockers, contradictory
   signals, or missing direct cause.

### Expected-Failure and Derivative-Failure Handling

- Before classifying a migration failure as unexpected, check whether the test is
  explicitly validating a failure path.
- If the code uses `pytest.raises(...)` around migration execution, do not treat the
  expected exception itself as a defect.
- If `expected_migration_result` is `fail`, or hook configuration declares
  `expected_result: fail`, then a failed migration may be the intended behavior.
- For expected hook-failure tests, only classify a defect if the ACTUAL failed step does
  not match the expected step, or if the follow-up validation behaves incorrectly.
- If an expected-failure test fails at the wrong step or for the wrong reason, classify
  the mismatch itself: wrong test expectation or harness logic is `CODE ISSUE`, while
  valid test expectations plus unexpected product behavior is `PRODUCT BUG`.
- Treat later `xfail` or runtime `skip` results as derivative when they are caused by the
  first failure in the class. Analyze the root cause, not the derivative outcome.

### CODE ISSUE - Test Framework, Test Code, or Test-Owned Configuration Problem

Indicators:

- Python import errors, syntax errors, or obvious `AttributeError` in test code
- Fixture setup or teardown failures such as missing fixtures, bad fixture data, or
  `fixture_store` misuse
- Incorrect assertions, stale expectations, or wrong expected values in validation logic
- Test configuration errors such as missing `py_config` keys, bad parameter values, or
  wrong provider credentials and URLs
- Incorrect use of `create_and_store_resource()` or `openshift-python-wrapper`
- SSH connection failures caused by wrong test-owned credentials, keys, or setup
- Timeouts caused by incorrect wait conditions or too-low test-owned timeouts
- Cleanup or isolation failures such as stale namespaces, plans, maps, or VMs from
  previous runs due to test logic
- Errors in test-owned helper logic when the underlying product state is healthy
- Expected-failure tests written incorrectly, such as wrong `pytest.raises(...)`,
  wrong expected migration result, or wrong expected hook step

### PRODUCT BUG - Actual MTV, Forklift, or Related Product Defect

Indicators:

- Migration, Plan, StorageMap, NetworkMap, or Provider CRs reject VALID configurations
  or enter invalid states
- `forklift-controller`, validation, or migration-related pods show product errors,
  panics, or crashes
- Migration remains stuck while controllers are healthy and the inputs are valid
- Post-migration VM validation shows the migrated VM is misconfigured despite valid
  source data and valid test expectations
- `forklift-inventory` returns incorrect or incomplete provider data while the source
  provider itself is healthy
- Warm migration precopy or cutover fails with valid configuration
- Copy-offload or XCOPY fails in a supported configuration with valid storage setup
- Guest agent, networking, disks, CPU, memory, or hooks behave incorrectly after
  migration even though the migration should have produced a valid VM
- Product finalizers or controllers fail to clean up resources they own
- Errors clearly originate from `forklift.konveyor.io` CRD controllers or admission
  logic after valid input has been provided

### Environmental Blockers and Ambiguous Cases

Infrastructure or lab failures are NOT confirmed `PRODUCT BUG` findings. Treat them as
environmental blockers with low confidence unless there is direct evidence that MTV
caused the instability.

Common environmental blockers:

- Cluster unreachable, OCP API timeout, or node `NotReady`
- DNS, routing, or network path outage
- Storage backend outage, PVC provisioning outage, or image pull failure
- Source provider endpoint unavailable or external authentication backend outage
- Remote cluster mismatch or unavailable remote cluster

Guidance:

- Classify a pure environmental blocker as `INFRASTRUCTURE`, never as a confirmed
  `PRODUCT BUG`.
- If the evidence only shows environment instability, say so explicitly and state low
  confidence in `details`.
- `skipif` or runtime `skip` caused by missing prerequisites is not a product defect.

When uncertain:

- Do not assume `utilities/` or `libs/` means `CODE ISSUE`. Those layers often surface
  provider, cluster, or product state.
- Do not assume product ownership only because the traceback touches product-facing
  resources. Look for direct evidence such as CR status, pod logs, pipeline steps,
  assertions, and provider state.
- If direct evidence is missing or contradictory, lower the confidence you state in
  `details` and list there exactly what information is missing.

### Upstream Causality Chain (MANDATORY for timeout, migration, and network failures)

A `TimeoutExpiredError` waiting on a migration phase, a pod status, or SSH connectivity is
SECONDARY. The timeout says WHAT did not happen; you must say WHY it did not happen.

Required investigation order:

1. **Identify the timed-out condition.** Which wait timed out, on which object, and between
   which two timestamps. Quote the wait target from the test (`Plan` status, `Migration`
   phase, `VirtualMachine` ready, SSH port open).
2. **Identify the component responsible for producing that condition.** `forklift-controller`
   for plan/migration orchestration, `forklift-inventory` for provider data, conversion pods
   for disk conversion, CDI/populator pods for transfer, `virt-launcher` for VM start.
3. **Read that component's ERROR logs inside the failure window** (operation start to
   observed failure). Use the `must-gather-analyzer` agent output when must-gather exists.
4. **Follow the chain upstream.** A layer-2 symptom (Kubernetes event: `connection reset`,
   `failed to create pod`, `context deadline exceeded`) is still a symptom. Keep going to the
   component log line that explains it.
5. **Stop at the deepest evidence that is actually available.** A clear product-path defect
   (wrong state transition, panic, rejected valid CR, retry loop) is a valid root cause — do
   not invent a deeper layer that is not in the artifacts.

Anti-patterns — these are not root causes:

- Reporting "migration timed out" or "connection reset by peer" as the cause
- Classifying from the test's `TimeoutExpiredError` without naming what the wait was on
- Stopping at Kubernetes events or CR conditions without reading component logs
- Blaming a too-low test timeout without evidence that the product progressed at all

The same rule applies to "migration stuck": name the pipeline step that never completed,
the pod or controller responsible for it, and the error that explains it.

### Exception and Pattern Signals

Repo-specific exceptions from `exceptions/exceptions.py` provide useful signals, but
they are not verdicts by themselves:

- `MigrationPlanExecError` - migration failed or timed out; decide whether the failure
  was expected, a test timeout is too low, or the product genuinely stalled
- `ForkliftPodsNotRunningError` - controller pods are unhealthy; this can be a product
  problem or an environmental blocker, not automatically a test bug
- `MigrationNotFoundError`, `MigrationStatusError`, `VmPipelineError` - migration CR is
  missing, incomplete, or lacks usable pipeline data; usually product or environment
- `VmNotFoundError` - determine whether source inventory lookup failed, the source VM is
  missing, or the migrated target state is inconsistent
- `VmMigrationStepMismatchError` - VMs in the same plan failed at different steps; this
  is not the root cause by itself, so inspect each VM pipeline
- `VmCloneError`, `VmMissingVmxError`, `VmBadDatastoreError` - source provider or source
  VM issues; can be configuration, environment, or product depending on evidence
- `MissingProvidersFileError` - missing test configuration, usually `CODE ISSUE`
- `SessionTeardownError` - cleanup failed after execution; usually a teardown or
  environment issue, not the primary migration failure
- `RemoteClusterAndLocalCluterNamesError` - remote or local cluster mismatch in test or
  environment configuration
- `OvirtMTVDatacenterNotFoundError`, `OvirtMTVDatacenterStatusError` - RHV environment
  or provider readiness issue unless evidence shows MTV mis-handled valid state
- `TimeoutExpiredError` from `timeout_sampler` - common wrapper signal; inspect WHAT
  timed out before classifying it

Pattern guidance:

- **Migration timeout:** Too-low timeout or wrong wait target is `CODE ISSUE`; a real
  stall with healthy inputs is `PRODUCT BUG`; an API or backend outage is environmental
- **Post-migration validation:** Wrong expected values are `CODE ISSUE`; a migrated VM
  with wrong disks, network, CPU, or memory is `PRODUCT BUG`
- **Provider connection failure:** Wrong credentials or URLs are `CODE ISSUE`; external
  provider outage is environmental; healthy provider plus broken MTV inventory is a
  `PRODUCT BUG`
- **Warm migration failure:** Bad precopy or cutover settings are `CODE ISSUE`; valid
  configuration plus controller failure is `PRODUCT BUG`
- **Copy-offload failure:** Wrong storage vendor configuration is `CODE ISSUE`;
  unsupported or unavailable backend is environmental; supported valid path failing is
  `PRODUCT BUG`
- **Resource creation failure:** Wrong parameters, name collisions, or test isolation
  problems are `CODE ISSUE`; valid CR rejected or stuck by product logic is `PRODUCT BUG`
- **SSH validation failure:** Wrong SSH credentials or keys are `CODE ISSUE`; migrated VM
  network or guest state broken by migration is `PRODUCT BUG`; routing outage is environmental
- **Resource leftovers:** Missing cleanup or bad fixture ownership is `CODE ISSUE`;
  product finalizer or controller cleanup failure is `PRODUCT BUG`; namespace or cluster
  cleanup blocked by infrastructure is environmental

### Jira Search Keyword Guidance

When classifying as `PRODUCT BUG`, fill `product_bug_report.jira_search_keywords` with 3-5
short keywords that combine a component with a concrete symptom:

- Good: `forklift-controller plan not ready`, `virt-v2v conversion guest boot`,
  `xcopy populator clone failed`, `CDI importer PVC pending`
- Bad: `timeout`, `error`, `migration failed` (too generic to find anything)

Never include test names, class names, fixture names, or `pytest` terms — those never
appear in product bug titles. Keep unselected domain candidates in the evidence, not in
the final keywords.

### Severity Definitions for Product Bugs

When classifying as `PRODUCT BUG`, set severity in `product_bug_report.severity` based on
impact:

- `critical` - data loss, VM corruption, cluster-wide disruption, security vulnerability
- `high` - VM cannot migrate, start, or be validated at all; PVC or data inaccessible;
  upgrade path blocked
- `medium` - feature degraded but a workaround exists; failure limited to a specific
  provider, storage backend, or plan configuration; intermittent under defined conditions
- `low` - cosmetic or diagnostic-only: misleading status message, wrong condition reason,
  log noise, documentation gap in CR status

### Code Fix Suggestions for Code Issues

When classifying as `CODE ISSUE`, name the fix concretely:

- The exact file and line where the change belongs
- The change itself (for example: "wait for `Plan` Ready before executing the migration",
  "store the value in a variable instead of recomputing it")
- An existing pattern in this repo that already does it correctly, when one exists

"Improve the wait" is not a fix suggestion. A reviewer must be able to apply it.

## 3. Analysis Thoroughness and Required Evidence Structure

**CRITICAL: Never dismiss or skip warnings, conditions, or errors found in the data.**
Every warning, condition entry, and error message in Plan, Migration, Provider, and
related resource status MUST be evaluated as a potential contributing factor.

### Environment Block (MANDATORY — first thing in `details`)

Every analysis opens `details` with the versions under test. A product bug claim without a
version is not actionable — reviewers cannot reproduce it and triage cannot route it.

Collect from `pytest-tests.log` (session header), the job console, and the cluster:
MTV/Forklift operator version (`oc get csv -n openshift-mtv`), OpenShift version,
source provider type and version, target storage backend and `StorageClass`, network type
(pod or multus with the NAD involved).

`details` MUST start with exactly this shape:

```text
Environment:
- MTV/Forklift: <csv version or unknown — not in artifacts> [HIGH|LOW]
- OpenShift: <version> [HIGH|LOW]
- Source provider: <vsphere 8.0 / rhv 4.5 / openstack / ova / remote openshift> [HIGH|LOW]
- Storage: <backend / StorageClass> [HIGH|LOW]
- Network: <pod | multus(NAD)> [HIGH|LOW]

Root Cause: <one line>
```

`[HIGH]` means the component is implicated in this failure, `[LOW]` that it is included for
context. When a version is genuinely unavailable, write `unknown — not in artifacts`; never
omit the line silently.

### Artifact Triage and Log Priority (MANDATORY)

Attached artifacts are advertised in a `BUILD ARTIFACTS (MANDATORY)` prompt section and
reachable as `build-artifacts/`; `AVAILABLE RESOURCES` lists repositories only. If no such
section exists for this run, no artifacts were attached — record that as a missing-information
case rather than assuming a bundle exists.

This suite collects artifacts on failure: `pytest-tests.log` (the run log, including
fixture teardown), enriched JUnit XML, and a must-gather bundle collected per Plan
(`utilities/must_gather.py`). Test configuration lives in `.providers.json` — never quote
credentials or secrets from it into the analysis.

Evidence priority, highest first:

1. **Component ERROR logs** — the actual error (controller, inventory, conversion,
   CDI/importer, populator, virt-launcher)
2. **Kubernetes events** — the reported error, usually downstream
3. **CR status and conditions** — Plan, Migration, Provider, `status.vms[].pipeline[]`
4. **Pod phases and container restarts** — when it failed
5. **Test console / assertion output** — how the test observed it

If the analysis cites only layers 2-5, the root cause has NOT been found — go back to
layer 1. Three cases are not failures of the analysis, and each must be stated explicitly
in the missing-information section rather than papered over:

- **No must-gather for this run** — say `must-gather: not collected` and proceed with what exists.
- **Plan-scoped bundle with no component logs** — per-Plan collection runs the targeted
gather (`/usr/bin/targeted`, `utilities/must_gather.py:run_plan_must_gather`), which keeps
that plan's VMs, networks and events rather than a full-cluster snapshot. A targeted bundle
that simply does not contain controller logs is expected; say
`targeted bundle — component logs not in scope` and proceed.
- **Full gather present** — then layer 1 exists and citing only layers 2-5 is a failed analysis.

Do not demand logs that were never collected, and do not treat a targeted bundle's silence
as evidence that the component was healthy.

### Regression Detection (MANDATORY when the failure looks new)

When the failure shows recent-onset or behavioural-change signals, use the STEP 0 project
agents and rootcoz's own failure history together:

- `regression-detector` — version correlation and a version boundary.
- `code-path-scanner` — the code paths implicated, plus the dependency versions actually
  pinned in the workspace. It has no shell and no git: additional repos are depth-1 clones,
  so it cannot produce commit history. Do not ask it for "what changed".
- **Failure history** — the authoritative signal for whether this test passed before. If the
  same test passed in earlier runs, treat it as a regression candidate regardless of what
  the agents reported.

Put the regression evidence inside `details`. Keep `pattern` as `NEW` at this stage; the
pattern classification step consumes your evidence. If the investigation found nothing,
state `Regression investigation: no evidence of a recent behavioural change in <component>`.
If the data needed for the assessment is missing, state
`Regression investigation: insufficient data to assess a recent behavioural change`.

### Self-Verification (MANDATORY — before submitting any analysis or revision)

1. Does `details` start with `Environment:` and list every version line?
2. Did you read and apply the STEP 0 project agents whose question applies to this failure
   (must-gather, regression, recent changes, cross-domain)?
3. Is `classification` exactly one of `CODE ISSUE`, `PRODUCT BUG`, `INFRASTRUCTURE`?
4. If `PRODUCT BUG`: did you reach layer-1 component evidence, or state that it was not
   collected? Did you fill `product_bug_report.jira_search_keywords` with 3-5 keywords?
5. If `CODE ISSUE`: does `code_fix` name a file, a line, and the change?
6. Did you evaluate every warning, condition, and error you saw, and give a reason for each
   one you ruled out?
7. On a revision after peer feedback: is the Environment block still the first block?

rootcoz stores the analysis in its `AnalysisDetail` schema, which has exactly these
fields:

- `classification` - one of `CODE ISSUE`, `PRODUCT BUG`, `INFRASTRUCTURE`
- `pattern` - `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, or `PERSISTENT`
- `affected_tests` - the failing test names
- `details` - the analysis narrative
- `artifacts_evidence` - verbatim log, status, or console lines you relied on
- `code_fix` - populated for `CODE ISSUE`
- `product_bug_report` - populated for `PRODUCT BUG`

Two of those fields are structured, and the detail goes one level down:

- `product_bug_report`: `title`, `severity` (`critical`/`high`/`medium`/`low`), `component`,
  `description`, `evidence` (verbatim log snippets), `jira_search_keywords` (list of
  3-5 strings). This is where severity and search keywords belong — not at the top level.
- `code_fix`: `file`, `line`, `change`, and optionally `original_code`. This is where the
  concrete fix belongs.

Any other top-level key is discarded. Everything below therefore belongs INSIDE the
`details` narrative:

- the confidence level, in words
- the primary evidence: the strongest direct observations supporting the classification,
  such as stack traces, controller errors, pipeline failures, or assertion mismatches
- secondary signals: relevant but non-decisive context, such as related warnings,
  preceding conditions, or environmental noise
- the warnings considered: the warnings or conditions you inspected, including Plan,
  Migration, Provider, and storage or network-related status when present
- why each warning was ruled out: the causal reason a warning is not related to the
  failure; do not dismiss a warning without a reason
- Multiple issues can coexist. Identify the PRIMARY cause and list secondary issues
  separately instead of collapsing them into one vague explanation
- Do not let a generic warning outweigh a direct failure signal unless you can demonstrate
  the causal path
- If the evidence is contradictory, say so explicitly and lower confidence

Example: A Plan condition warning about unsupported IP preservation with Pod Networking
may be secondary to a `ConvertGuest` failure, or it may point to a misconfigured plan.
Investigate it explicitly before ruling it out.

## 4. Missing Information Guidance

**CRITICAL: For EVERY analysis, if the provided error, stack trace, or console output**
**lacks enough detail for a confident diagnosis, you MUST include a missing-information**
**section inside `details` describing what additional data would help.**

### For `CODE ISSUE`, suggest collecting

- Full fixture chain output showing which fixture failed and why
- Test configuration dump, including relevant `py_config` values
- Specific `@pytest.mark.parametrize` values for the failing class or method
- Related helper or utility source code if the failing path is not already visible
- Assertion text, expected values, and actual values for the failing validation

### For `PRODUCT BUG`, suggest collecting

- `forklift-controller` logs:
  `oc logs -n openshift-mtv deployment/forklift-controller`
- Migration CR status:
  `oc get migration <name> -n <namespace> -o yaml`
- Plan CR status:
  `oc get plan <name> -n <namespace> -o yaml`
- Provider CR status:
  `oc get provider <name> -n <namespace> -o yaml`
- VM pipeline details from `status.vms[].pipeline[]`
- Must-gather data:
  `oc adm must-gather --image=quay.io/kubev2v/forklift-must-gather:latest`
- Target VM status:
  `oc get vm <name> -n <namespace> -o yaml`
- CDI DataVolume and PVC status for disk transfer issues
- Source provider logs such as vCenter events, RHV engine logs, or OpenStack nova logs
- Network attachment definitions and storage class details when mapping is involved
- Events in the target namespace:
  `oc get events -n <namespace> --sort-by='.lastTimestamp'`

### For environmental blockers, suggest collecting

- Cluster node status:
  `oc get nodes`
- Cluster operator status:
  `oc get co`
- OpenShift version:
  `oc version`
- MTV operator version:
  `oc get csv -n openshift-mtv`
- Provider endpoint health, DNS reachability, storage backend health, and other external
  dependency status that could explain a lab or infrastructure outage

## 5. Key Components and Test Stack

- **Test framework:** `pytest` with `pytest-testconfig`, `@pytest.mark.incremental`,
  and optionally `pytest-xdist`
- **OpenShift interactions:** `openshift-python-wrapper` via `ocp_resources.*` and
  `ocp_utilities.*`; direct runtime use of the `kubernetes` client is not expected
- **Provider SDKs:** `pyVmomi` for VMware, `ovirtsdk4` for RHV, and `openstacksdk`
  for OpenStack
- **SSH validation:** `paramiko`
- **Resource management:** All OpenShift resources should be created through
  `create_and_store_resource()`

Key product and runtime components to inspect:

| Component | Role in failure analysis | First place to inspect |
| --- | --- | --- |
| `forklift-controller` | Orchestrates migrations | Pod logs in `openshift-mtv` |
| `forklift-inventory` | Source inventory sync | Pod logs and provider sync status |
| Validation / admission | Rejects invalid CRs | Controller logs, webhook errors |
| `virt-v2v` | VM disk conversion | Migration pod logs in target namespace |
| `CDI` | DataVolume/PVC workflows | DataVolumes, PVCs, CDI pod logs |
| `KubeVirt` | Resulting VM runtime | `VirtualMachine`, `VMI`, launcher pod status |
| Provider / Map / Plan / Migration / Hook CRs | Declarative control-plane state | `status`, conditions, per-VM pipeline |

## 6. Project Agents and When to Use Them

rootcoz advertises these as STEP 0 project agents under `.rootcoz/agents/` and copies them
to `.pi/agents/` in the analysis workspace. They are not separate processes: they are
instructions you must read and apply. Read the ones whose question matches the failure, and
use their output as evidence — do not redo their investigation.

| Agent | Answers | Use when |
| --- | --- | --- |
| `must-gather-analyzer` | What do the collected logs actually say, and how deep did the evidence reach? | A `BUILD ARTIFACTS` section advertises an artifacts directory |
| `regression-detector` | Is this a regression, with which version boundary? | Failure looks recent-onset or behavioural |
| `code-path-scanner` | Which code paths produced this failure, and which dependency versions are pinned? | A shared helper, fixture, or wrapper is implicated |
| `cross-domain-resolver` | Which product domains are involved, and what Jira keywords find them? | Classifying as `PRODUCT BUG` |

The separate history prompt `.rootcoz/ROOTCOZ_HISTORY_PROMPT.md` answers a different
question — whether an earlier test's failed teardown caused this failure — and takes
precedence over all of the above when it applies.

## 7. Reference Links

- Product docs: [MTV Documentation][mtv-doc],
  [OpenShift Virtualization Documentation][ocp-virt-doc]
- Upstream: [kubev2v/forklift][forklift-repo],
  [kubev2v/forklift-console-plugin][forklift-console],
  [kubev2v/forklift-must-gather][forklift-must-gather],
  [kubev2v/forklift-documentation][forklift-docs],
  [Copy-Offload (XCOPY) Volume Populator][xcopy-populator]
- Test infra and CRDs: [RedHatQE/openshift-python-wrapper][opw-repo],
  [openshift-python-wrapper API docs][opw-docs],
  [Forklift CRDs source][forklift-crds],
  [Hooks documentation][hooks-docs]

[mtv-doc]: https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/
[ocp-virt-doc]: https://docs.redhat.com/en/documentation/red_hat_openshift_virtualization/
[forklift-repo]: https://github.com/kubev2v/forklift
[forklift-console]: https://github.com/kubev2v/forklift-console-plugin
[forklift-must-gather]: https://github.com/kubev2v/forklift-must-gather
[forklift-docs]: https://github.com/kubev2v/forklift-documentation
[xcopy-populator]: https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator
[opw-repo]: https://github.com/RedHatQE/openshift-python-wrapper
[opw-docs]: https://redhatqe.github.io/openshift-python-wrapper/
[forklift-crds]: https://github.com/kubev2v/forklift/tree/main/operator/config/crd/bases
[hooks-docs]: https://github.com/kubev2v/forklift/blob/main/docs/hooks.md

## FINAL REMINDER — READ THIS LAST

**`details` starts with the `Environment:` block. Every time. On revisions too.**

And the two failure modes this prompt exists to prevent:

- Reporting the test's timeout, event, or exception text as the root cause — the root cause
  is the component error one layer below it, or an explicit statement that it was not
  collected.
- Reporting a cascade of downstream steps as separate defects — if an earlier teardown broke
the state, that is ONE root cause, and every later failure inherits its classification.
