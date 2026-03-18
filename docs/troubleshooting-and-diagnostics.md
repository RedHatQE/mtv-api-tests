# Troubleshooting And Diagnostics

When a migration test fails, the fastest path is usually:

1. Read `pytest-tests.log` to find the first real failure.
2. Open `junit-report.xml` to see the CI-friendly result and embedded logs.
3. Inspect `.data-collector/` for tracked resources and any must-gather output.
4. Follow the resource names into the cluster: `Plan`, `Migration`, `Provider`, target namespace pods, and events.

This repository already generates most of those artifacts for you by default.

## Start Here

The repo enables JUnit reporting for every run and includes pytest logging in the XML:

```4:25:pytest.ini
addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
  --junit-xml=junit-report.xml
  --basetemp=/tmp/pytest
  --show-progress
  --strict-markers
  --jira
  --dist=loadscope

markers =
    tier0: Core functionality tests (smoke tests)
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))

junit_logging = all
```

That means a normal run gives you:

- `pytest-tests.log`: the easiest way to see fixture setup, `SETUP` / `CALL` / `TEARDOWN`, and final `PASSED` / `FAILED` / `ERROR` status.
- `junit-report.xml`: the structured result file that CI systems can ingest.
- Console output with the same high-level status markers.

If you run tests inside a container or an OpenShift Job, the default working directory is `/app`, so the JUnit file usually ends up at `/app/junit-report.xml`.

> **Tip:** Test names are rewritten during collection to include the selected `source_provider` and `storage_class`, so those suffixes are useful when you are matching a JUnit failure back to the exact environment that ran.

## Diagnostic Flags That Matter

A few pytest options change what diagnostics you get back:

- `--analyze-with-ai` enables post-failure AI enrichment of the JUnit XML.
- `--skip-data-collector` disables automatic artifact collection under `.data-collector/`.
- `--data-collector-path` changes the artifact directory from the default `.data-collector`.
- `--skip-teardown` leaves created resources in place so you can inspect them live.
- `--openshift-python-wrapper-log-debug` turns on deeper wrapper-level logging, which is useful when CR creation or wait logic is failing.

> **Warning:** `--skip-data-collector` disables both `resources.json` tracking and automatic must-gather collection.

> **Warning:** `--skip-teardown` is very useful for debugging, but it also means cleanup becomes your responsibility.

> **Tip:** If the failure is happening before MTV even starts a migration, `--openshift-python-wrapper-log-debug` often gives the most useful extra detail.

## JUnit Output

`junit-report.xml` is the main machine-readable artifact. It is the best file to archive from local runs, OpenShift Jobs, or any external automation.

Because `junit_logging = all` is enabled, the report is more useful than a bare pass/fail summary. It includes the logs that explain whether the failure happened during:

- fixture setup
- plan creation
- migration execution
- post-migration validation
- teardown

A passing example in the repo is very small:

```1:1:junit_report_example.xml
<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" time="231.588" timestamp="2021-09-15T02:34:21.557789" hostname="fedora"><testcase classname="test_mtv" name="test_mtv_migration_interop[plans0]" time="231.539" /></testsuite></testsuites>
```

In real failure runs, the XML is much more informative because it includes logging and failure details from pytest.

## AI Enrichment

When you pass `--analyze-with-ai`, the suite can send the raw JUnit XML to an analysis service and write the enriched XML back to the same file.

```423:479:utilities/pytest_utils.py
xml_path_raw = getattr(session.config.option, "xmlpath", None)
if not xml_path_raw:
    LOGGER.warning("xunit file not found; pass --junitxml. Skipping AI analysis enrichment")
    return

xml_path = Path(xml_path_raw)
if not xml_path.exists():
    LOGGER.warning(
        "xunit file not found under %s. Skipping AI analysis enrichment",
        xml_path_raw,
    )
    return

# ... provider/model validation omitted ...

response = requests.post(
    f"{server_url.rstrip('/')}/analyze-failures",
    json={
        "raw_xml": raw_xml,
        "ai_provider": ai_provider,
        "ai_model": ai_model,
    },
    timeout=timeout_value,
)
response.raise_for_status()
result = response.json()

if enriched_xml := result.get("enriched_xml"):
    xml_path.write_text(enriched_xml)
    LOGGER.info("JUnit XML enriched with AI analysis: %s", xml_path)
```

Important behavior:

- AI enrichment only runs when the session exits with failures.
- It requires a JUnit XML path to exist.
- It requires `JJI_SERVER_URL`.
- If `JJI_AI_PROVIDER` and `JJI_AI_MODEL` are not set, the code defaults them to `claude` and `claude-opus-4-6[1m]`.
- `JJI_TIMEOUT` controls the request timeout and defaults to `600` seconds.
- If enrichment fails, the original JUnit file is preserved.

> **Note:** If `JJI_SERVER_URL` is not set, the suite logs a warning and disables AI analysis instead of failing the run.

> **Warning:** AI enrichment rewrites the existing `junit-report.xml` in place.

## Collected Resource Artifacts

Unless you use `--skip-data-collector`, the suite prepares a clean `.data-collector/` directory at session start and writes run artifacts there.

The most important files are:

- `.data-collector/resources.json`: a dump of tracked resources created during the run.
- `.data-collector/<pytest-node-name>/...`: per-failure must-gather output when the failure hook runs.
- `.data-collector/...` at the root: session-level must-gather output when teardown cleanup fails.

The tracked resource file is especially useful after a partial or messy failure because it tells you exactly which names and namespaces were created. The cleanup helper in `tools/clean_cluster.py` is built to consume that file.

The suite stores resource metadata as it creates objects, including `name`, `namespace`, `module`, and sometimes `test_name`. That gives you a practical bridge from “the test failed” to “which exact `Plan`, `Provider`, `StorageMap`, or `NetworkMap` should I inspect?”

> **Tip:** If you rerun the test suite, the base data collector path is recreated. Move or archive old artifacts first if you want to keep them.

## Must-Gather Support

The suite has built-in must-gather support. When it can match a failure to a specific plan, it can run a targeted gather. Otherwise it falls back to an MTV-namespace gather.

```166:181:utilities/must_gather.py
must_gather_image = _resolve_must_gather_image(
    ocp_admin_client=ocp_admin_client,
    mtv_subs=mtv_subs,
    mtv_csv=mtv_csv,
)

_must_gather_base_cmd = f"oc adm must-gather --image={must_gather_image} --dest-dir={data_collector_path}"

if plan:
    plan_name = plan["name"]
    plan_namespace = plan["namespace"]
    run_command(
        shlex.split(f"{_must_gather_base_cmd} -- NS={plan_namespace} PLAN={plan_name} /usr/bin/targeted")
    )
else:
    run_command(shlex.split(f"{_must_gather_base_cmd} -- -- NS={mtv_namespace}"))
```

What this means in practice:

- If the failure can be tied back to a specific plan, the gather can be scoped to that plan.
- If the failure happens earlier, or there is no plan match, the gather is scoped to the MTV namespace instead.
- If teardown leaves leftovers behind, a session-level must-gather is collected as a fallback.

> **Note:** The must-gather image is not hardcoded. The code resolves it from the installed MTV operator CSV and ImageDigestMirrorSet, so the gather matches the cluster’s installed MTV build.

> **Tip:** A per-test must-gather directory is often the quickest way to answer “what did the cluster look like at the moment this test failed?”

## Common Failure Points

Most failures in this repo fall into one of these buckets.

### Before Migration Starts

- Missing required config such as `storage_class` or `source_provider`.
- Missing or empty `.providers.json`.
- A provider key passed with `--tc=source_provider:...` that does not exist in `.providers.json`.
- Wrong cluster credentials from `cluster_host`, `cluster_username`, or `cluster_password`.
- SSL verification mismatches between your config and the created provider secret.
- `forklift-*` pods not being healthy before tests begin.

These failures usually show up in fixtures or session startup, before you ever get a `Migration` CR.

### During Provider, Map, or Plan Setup

- The source `Provider` CR never becomes ready.
- The source provider endpoint is unreachable or credentials are wrong.
- The source VM is missing from inventory.
- The source VM has no networks, so `NetworkMap` generation fails.
- `StorageMap` points to the wrong storage class or invalid copy-offload datastores.
- A remote-cluster configuration mismatch prevents the OpenShift client setup from proceeding.

These issues usually show up as setup failures, plan readiness timeouts, or early MTV resource errors.

### During Migration Execution

- The `Plan` becomes ready, but the migration never reaches `Succeeded`.
- The migration reaches `Failed`.
- The run times out waiting for migration completion.
- Hook execution fails at `PreHook` or `PostHook`.
- Warm migration timing is wrong for the environment.

The repo’s default timeout config is important here:

- `plan_wait_timeout` defaults to `3600` seconds.
- `mins_before_cutover` defaults to `5`.

If a migration is stuck, the most important resources are the `Plan`, the `Migration`, and the relevant MTV controller or conversion pod logs.

### After Migration Completes

A migration can succeed and the test can still fail later. The post-migration validation is broad and covers things like:

- VM power state
- CPU and memory
- network mapping
- storage mapping and storage class
- PVC naming
- guest agent
- SSH access
- static IP preservation
- labels
- affinity
- node placement
- VMware snapshot and serial preservation
- RHV-specific power-off behavior

So “migration failed” and “test failed” are not always the same thing.

> **Tip:** If the test only fails in `test_check_vms`, the migration itself may already be complete. At that point, spend less time in the initial `Plan` conditions and more time on the migrated VM, its PVCs, its launcher/VMI state, and guest-level checks.

## Copy-Offload-Specific Checks

Copy-offload adds extra prerequisites, so it also adds extra failure modes.

```27:58:.providers.json.example
"copyoffload": {
  # Supported storage_vendor_product values:
  # - "ontap"           (NetApp ONTAP)
  # - "vantara"         (Hitachi Vantara)
  # - "primera3par"     (HPE Primera/3PAR)
  # - "pureFlashArray"  (Pure Storage FlashArray)
  # - "powerflex"       (Dell PowerFlex)
  # - "powermax"        (Dell PowerMax)
  # - "powerstore"      (Dell PowerStore)
  # - "infinibox"       (Infinidat InfiniBox)
  # - "flashsystem"     (IBM FlashSystem)
  "storage_vendor_product": "ontap",

  # Primary datastore for copy-offload operations (required)
  "datastore_id": "datastore-12345",

  # Optional: Secondary datastore for multi-datastore copy-offload tests
  "secondary_datastore_id": "datastore-67890",

  # Optional: Non-XCOPY datastore for mixed datastore tests
  "non_xcopy_datastore_id": "datastore-99999",

  "default_vm_name": "rhel9-template",
  "storage_hostname": "storage.example.com",
  "storage_username": "admin",
  "storage_password": "your-password-here",  # pragma: allowlist secret
```

Check these first for copy-offload failures:

- `storage_vendor_product` is correct for your storage backend.
- `datastore_id` is valid and matches where the source VM disks actually live.
- `secondary_datastore_id` and `non_xcopy_datastore_id` are only used when the test really needs them.
- Storage credentials are present either in `.providers.json` or the matching `COPYOFFLOAD_*` environment variables.
- Vendor-specific fields are set when your selected storage vendor requires them.
- If you use SSH cloning, the ESXi host, user, and password are correct.
- If the offload path itself is the problem, inspect `vsphere-xcopy-volume-populator` logs in `openshift-mtv` in addition to the normal MTV controller logs.

> **Warning:** Copy-offload failures are often configuration issues first, product issues second. Always verify the storage backend, datastore IDs, and vendor-specific fields before assuming the migration code is at fault.

## Expected Failures and Hook Tests

Not every `MigrationPlanExecError` means something is wrong. This repo contains tests that intentionally expect migration failure and then validate how that failure happened.

A good example is `test_post_hook_retain_failed_vm`, which expects the migration to fail and then checks whether the failure happened at the expected hook step:

```195:205:tests/test_post_hook_retain_failed_vm.py
expected_result = prepared_plan["expected_migration_result"]

if expected_result == "fail":
    with pytest.raises(MigrationPlanExecError):
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )
    self.__class__.should_check_vms = validate_hook_failure_and_check_vms(self.plan_resource, prepared_plan)
```

Why this matters:

- Some tests deliberately expect `PreHook` or `PostHook` failure.
- A `PostHook` failure can still leave a migrated VM behind, and that VM is worth inspecting.
- The real question is often not “did it fail?” but “did it fail at the expected step?”

> **Tip:** In class-based incremental tests, focus on the first real failure. Later tests in the same class may be marked `xfail` because the earlier step already failed.

## Best Places To Inspect When Migrations Fail

| Inspect This | What It Tells You | Best For |
| --- | --- | --- |
| `pytest-tests.log` | First failing phase, fixture flow, high-level status | Early setup failures and quick triage |
| `junit-report.xml` | Structured results, embedded logs, optional AI analysis | CI artifacts and cross-run comparisons |
| `.data-collector/resources.json` | Exact resource names and namespaces created during the run | Finding or cleaning up leftovers |
| `.data-collector/<failing-test>/` | Must-gather output captured near the failure | Deep cluster-side diagnosis |
| `Plan` CR | Readiness, mappings, target namespace, conditions | Plan creation and migration start issues |
| `Migration` CR | Per-VM pipeline details in `status.vms[].pipeline[]` | Mid-migration failures and hook step debugging |
| `Provider` CR | Connection and readiness status | Source or destination connectivity problems |
| `forklift-controller` logs | MTV orchestration and controller-side errors | Plan or migration logic failures |
| `forklift-inventory` logs | Inventory sync and source discovery issues | VM lookup and provider inventory problems |
| Target namespace events and pods | `virt-v2v`, DV/PVC/PV, VM/VMI, launcher behavior | Transfer, boot, and runtime issues |
| Source provider logs | vCenter, RHV, or OpenStack-side errors | External provider problems |

A practical first command set is:

- `oc logs -n openshift-mtv deployment/forklift-controller`
- `oc get migration <name> -n <namespace> -o yaml`
- `oc get plan <name> -n <namespace> -o yaml`
- `oc get provider <name> -n <namespace> -o yaml`
- `oc get vm <name> -n <namespace> -o yaml`
- `oc get events -n <namespace> --sort-by='.lastTimestamp'`

For copy-offload runs, also inspect the volume populator logs in `openshift-mtv`.

## A Good Debugging Order

If you want one repeatable process, use this:

1. Open `pytest-tests.log` and identify the first failing test and phase.
2. Check whether the failure is an expected one, especially in hook tests or incremental classes.
3. Read `junit-report.xml` for the same test case and capture the resource names involved.
4. Open `.data-collector/resources.json` and any must-gather output under `.data-collector/`.
5. Inspect the `Plan`, `Migration`, and `Provider` CRs.
6. Check `forklift-controller`, `forklift-inventory`, and target-namespace pod logs.
7. If the migration succeeded but the test still failed, inspect the migrated VM, its PVCs, its VMI/launcher state, and guest-level connectivity instead of only looking at the controller.

That order lines up with how this repository itself reports and classifies failures, and it usually gets you to the root cause faster than starting from cluster logs alone.
