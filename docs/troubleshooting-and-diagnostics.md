# Troubleshooting And Diagnostics

When a migration test fails, the fastest path is usually:

1. Read `pytest-tests.log` to find the first real failure.
2. Open `junit-report.xml` to see the CI-friendly result and embedded logs.
3. Inspect `.data-collector/` for tracked resources and any must-gather output.
4. Follow the resource names into the cluster: `Plan`, `Migration`, `Provider`, target namespace pods, and events.

This repository already generates most of those artifacts for you by default.

## Start Here

The repo enables JUnit reporting for every run and includes pytest logging in the XML:

```1:38:pytest.ini
[pytest]
testpaths = tests

addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
  --junit-xml=junit-report.xml
  --show-progress
  --strict-markers
  --dist=loadscope

markers =
    tier0: Core functionality tests (smoke tests)
    tier1: Extended functionality tests
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    copyoffload_sanity: Copy-offload sanity tests - core scenarios for quick validation
    copyoffload_snapshots: Copy-offload snapshot tests (vSphere only)
    shared_disk: Shared disk migration tests (vSphere only)
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))
    ova: OVA provider-specific tests
    vsphere: vSphere provider-specific tests
    esxi: ESXi provider-specific tests
    rhv: RHV provider-specific tests
    hyperv: Hyper-V provider-specific tests
    openstack: OpenStack provider-specific tests
    openshift: OpenShift provider-specific tests
    deep_inspection: Deep Inspection / Conversion CR tests (vSphere only)
    ca_crt: CA certificate field (ca.crt) provider secret tests
    aap: AAP (Ansible Automation Platform) hook integration tests (vSphere only)
    upgrade: MTV operator upgrade tests

junit_logging = all
```

That means a normal run gives you:

- `pytest-tests.log`: the easiest way to see fixture setup, `SETUP` / `CALL` / `TEARDOWN`, and final `PASSED` / `FAILED` / `ERROR` status.
- `junit-report.xml`: the structured result file that CI systems can ingest.
- Console output with the same high-level status markers.

`pytest-tests.log` is not a `pytest.ini` setting. `conftest.py` deletes any stale copy at session start and defaults the path to
`pytest-tests.log` in the working directory; override it with pytest's own `--log-file` option.

If you run tests inside a container or an OpenShift Job, the default working directory is `/app`, so the JUnit file usually ends up at `/app/junit-report.xml`.

> **Note:** `pytest-jira` is an installed dependency, but `--jira` is **not** in `addopts`. JUnit XML is the only reporting format enabled by default.
>
> **Note:** `--strict-markers` is on, so a test using a marker that is not listed in `pytest.ini` errors at collection time instead of running unfiltered.
>
> **Tip:** Test names are rewritten during collection to include the selected `source_provider` and `storage_class`, so those suffixes are useful when you are matching a JUnit
> failure back to the exact environment that ran.

## Diagnostic Flags That Matter

A few pytest options change what diagnostics you get back:

- `--analyze-with-ai` enables post-failure AI enrichment of the JUnit XML through a rootcoz server.
- `--skip-data-collector` disables automatic artifact collection under `.data-collector/`.
- `--data-collector-path` changes the artifact directory from the default `.data-collector`.
- `--skip-teardown` leaves created resources in place so you can inspect them live.
- `--openshift-python-wrapper-log-debug` turns on deeper wrapper-level logging, which is useful when CR creation or wait logic is failing.
- `--providers-json` points at a providers file other than the default, which is the fastest way to tell "wrong file" from "wrong provider".
- `--mtv-upgrade-timeout` sets the outer wait budget of the upgrade script; raise it when an upgrade legitimately takes longer than 90 minutes.

`--openshift-python-wrapper-log-debug` works by exporting `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG` into the environment at session start,
so it affects every wrapper log record in the run, not just a single test.

`--providers-json` resolution order is: the CLI flag, then the `PROVIDERS_JSON_PATH` environment variable, then `.providers.json` in the
current working directory. That same order is what `mtv-api-tests generate` and `mtv-api-tests run` use when they read the providers file.

> **Warning:** `--skip-data-collector` disables both `resources.json` tracking and automatic must-gather collection.
>
> **Warning:** `--skip-teardown` is very useful for debugging, but it also means cleanup becomes your responsibility.
>
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

When you pass `--analyze-with-ai`, the suite posts the raw JUnit XML to a **rootcoz** server and writes the enriched XML back to the same
file. rootcoz is an external service, not part of this repository; this page only documents the client half of the contract.

```499:554:utilities/pytest_utils.py
def enrich_junit_xml(session: pytest.Session) -> None:
    """Read JUnit XML, send to server for analysis, write enriched XML back.

    Reads the JUnit XML that pytest generated, POSTs the raw content to the
    rootcoz server's /analyze-failures endpoint, and writes the enriched XML
    (with analysis results) back to the same file.

    Args:
        session: The pytest session containing config options.
    """
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

    server_url = os.environ["ROOTCOZ_SERVER_URL"]
    raw_xml = xml_path.read_text()

    try:
        timeout_value = int(os.environ.get("ROOTCOZ_TIMEOUT", "600"))
    except ValueError:
        LOGGER.warning("Invalid ROOTCOZ_TIMEOUT value, using default 600 seconds")
        timeout_value = 600

    # Optional overrides; when omitted, rootcoz uses .rootcoz/settings.json
    payload: dict[str, str] = {"raw_xml": raw_xml}
    if ai_provider := os.environ.get("ROOTCOZ_AI_PROVIDER"):
        payload["ai_provider"] = ai_provider
    if ai_model := os.environ.get("ROOTCOZ_AI_MODEL"):
        payload["ai_model"] = ai_model

    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/analyze-failures",
            json=payload,
            timeout=timeout_value,
        )
        response.raise_for_status()
        result = response.json()
    except Exception as ex:
        LOGGER.exception(f"Failed to enrich JUnit XML, original preserved. {ex}")
        return

    if enriched_xml := result.get("enriched_xml"):
        xml_path.write_text(enriched_xml)
        LOGGER.info("JUnit XML enriched with AI analysis: %s", xml_path)
    else:
        LOGGER.info("No enriched XML returned (no failures or analysis failed)")
```

Important behavior:

- AI enrichment only runs when the session exits with a non-zero code.
- It is force-disabled under `--collect-only` and `--setup-plan`, which is what makes the `tox -e pytest-check` environments safe to run.
- It requires a JUnit XML path to exist.
- It requires `ROOTCOZ_SERVER_URL`.
The client-side environment variables are:

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ROOTCOZ_SERVER_URL` | Yes | - | rootcoz server base URL; the suite appends `/analyze-failures` |
| `ROOTCOZ_AI_PROVIDER` | No | applied server-side | Optional provider override included in the request body |
| `ROOTCOZ_AI_MODEL` | No | applied server-side | Optional model override included in the request body |
| `ROOTCOZ_TIMEOUT` | No | `600` | Request timeout in seconds |

Those defaults are **not** hardcoded in pytest. When `ROOTCOZ_AI_PROVIDER` and `ROOTCOZ_AI_MODEL` are unset, the request body simply omits
those keys and rootcoz falls back to `.rootcoz/settings.json` in this repository. That file holds the provider and model, the AI call timeout,
peer AI configs, and the list of `additional_repos` rootcoz may consult about this project.

Environment variables are read after `load_dotenv()`, so a `.env` file in the project root is enough — nothing has to be exported.

> **Note:** If `ROOTCOZ_SERVER_URL` is not set, the suite logs `ROOTCOZ_SERVER_URL is not set. Analyze with AI features will be disabled.`
> and turns the feature off instead of failing the run.
>
> **Warning:** AI enrichment rewrites the existing `junit-report.xml` in place. Archive a copy first if you need the raw file.

## Collected Resource Artifacts

Unless you use `--skip-data-collector`, the suite prepares a clean `.data-collector/` directory at session start and writes run artifacts there.

The most important files are:

- `.data-collector/resources.json`: a dump of tracked resources created during the run.
- `.data-collector/session-start/cluster.txt`: the cluster baseline captured at session start — `ClusterVersion`, a `ClusterOperator` summary, the `MachineConfigPool` counters and conditions (summaries, not the pool manifests), a `Node` summary, and the installed MTV and OpenShift Virtualization CSV versions
  and conditions, nodes, and the `openshift-mtv` / `openshift-cnv` CSVs. It answers "was the cluster already unhealthy before this run?". The file is written once per run by
  the xdist controller (or the sole process without xdist) before any worker starts, and workers never wipe or rewrite it. The probes read the cluster through the run's
  `DynamicClient` and the `ocp_resources` wrappers, so no `oc` binary and no kubeconfig are involved; a client is built from the resolved credentials when none is passed in,
  and if that fails the baseline is logged and skipped, never raised. Still set `--skip-data-collector` to disable it.
- `.data-collector/session-start/run-identity.txt`: the checkout revision read from git's own files (no subprocess), the pytest invocation, the authenticated user, and whether
  `--skip-teardown` / `--skip-data-collector` were set. Written once per run, like `cluster.txt`. Credentials never reach this file: the value of every credential-ish `--tc`
  (`password`, `token`, `secret`, `api_key`, `apikey`, `credential`), any URL userinfo (`scheme://user:pass@host` → `scheme://***@host`) and the resolved cluster password are
  replaced with `***`. Both `--tc=key:value` and the split `--tc key:value` forms are covered.
- `.data-collector/cluster-state/<timestamp>-<scope>-<reason>[-<worker>].txt`: bounded cluster snapshots written just before a real failure is raised (see
  [Failure Evidence Dumps](#failure-evidence-dumps)).
- `.data-collector/upgrade-script-<timestamp>/upgrade-script-stdout.log` and `-stderr.log`: the upgrade script's own output with `CLUSTER_PASSWORD` redacted. The child always writes
  into a temporary directory that is deleted before the upgrade returns, so the raw bytes never reach the artifact tree — and never the inherited streams either, because the
  script receives the cluster password and pytest runs with `-s`. Without `--data-collector-path` the redacted logs live under the default `.data-collector/`;
  with `--skip-data-collector` nothing is persisted at all, while the failure message still carries the redacted tails.
- `.data-collector/<encoded-class-plan-identity>/...`: must-gather output per failing class-plan instance per worker. The identity includes the class node ID, class-scoped parameter indices and worker ID.
- `.data-collector/<encoded-test-node-identity>/...`: must-gather output per failing standalone test per worker. Identities use reversible percent encoding to avoid path collisions.
- `.data-collector/...` at the root: session-level must-gather output when teardown cleanup fails.

The tracked resource file is especially useful after a partial or messy failure because it tells you exactly which names and namespaces
were created. The cleanup helper in `tools/clean_cluster.py` consumes that file directly:

```bash
python tools/clean_cluster.py .data-collector/resources.json
```

It reads each entry's `module` and resource kind, imports the matching wrapper class, and calls `clean_up()` on each object.

The suite stores resource metadata as it creates objects, including `name`, `namespace`, `module`, and sometimes `test_name`. That gives you a practical bridge from “the test
failed” to “which exact `Plan`, `Provider`, `StorageMap`, or `NetworkMap` should I inspect?”

> **Tip:** If you rerun the test suite, the base data collector path is recreated. Move or archive old artifacts first if you want to keep them.

## Failure Evidence Dumps

`utilities/cluster_state.py::dump_cluster_state()` writes a bounded cluster snapshot immediately before a real failure is raised, so the analyzer never has to re-run the
failing job to learn the cluster state. It is observation only and never raises: every probe reads the cluster through an `ocp_resources` wrapper on the run's `DynamicClient`
(never the `oc` binary, never a generated kubeconfig), each probe is guarded on its own and a failure is logged into the snapshot itself, and the original exception is always the
error you see. Each invocation writes one file, `cluster-state/<UTC timestamp>-<scope>-<reason slug>[-<xdist worker>].txt`, so parallel workers cannot collide.

Two limits keep a snapshot from delaying the error it documents:

- **Total deadline.** One snapshot has 120 seconds of budget in total (`_SNAPSHOT_DEADLINE_SECONDS`, measured with `monotonic()`). Probes that would start after it expires are
  not run: the file ends with `# snapshot deadline reached after <N>s of 120s, <M> probes skipped`. A short snapshot is therefore visibly truncated, never silently mistaken for a
  healthy cluster.
- **Size caps, per probe and per snapshot.** Each probe's rendered output is capped at 512 KiB (`_MAX_PROBE_OUTPUT_BYTES`); a longer record keeps its head and closes with
  `[truncated: <N> bytes omitted]`, and every container log keeps a bounded tail rather than the whole stream. A whole snapshot is additionally capped at 8 MiB
  (`_MAX_SNAPSHOT_OUTPUT_BYTES`): once it is exhausted no further probe runs and the file closes with `# snapshot size budget reached: <M> records not collected`. The
  per-node daemon pods make the log volume grow with the cluster size, so the snapshot budget — not the per-probe cap — is what keeps a large cluster's dump bounded.

Collection has no fallback path: the probe set is the wrapper's own API calls, and a run that passes no client (for example `run_mtv_upgrade()` without one) skips the snapshot
instead of reaching for a CLI. Under `--skip-data-collector` the collector path is resolved first and the dump returns before the first probe, so skip mode costs no API request.
Every artifact is written with the resolved `CLUSTER_PASSWORD` redacted, and the header records that the data came from the run `DynamicClient` through `ocp_resources` wrappers.

| Scope | Where it fires | What it collects |
| --- | --- | --- |
| `upgrade` | The upgrade script hits the outer wait budget, or exits non-zero after its own wait times out | `ClusterVersion`, `ClusterOperator` status plus the `machine-config` operator body, `MachineConfigPool` objects plus per-pool `ready`/`updated`/`degraded`/`unavailable` counters and conditions, nodes, Machine Config Operator pods with every container log (current and previous), `ImageDigestMirrorSet`, `ImageContentSourcePolicy`, the cluster-wide `Image` config, and the `openshift-mtv` CSVs |
| `storage` | A migration wait times out, or a migrated VM is confirmed unschedulable | the target namespace's PVCs, DataVolumes and PersistentVolumes plus the full body of the non-Bound PVCs only, namespace events oldest first, storage classes, `openshift-storage` pods, and the current plus previous logs of the ceph-csi `csi-provisioner` and `csi-rbdplugin` containers (3000-line tails on the controller plugin, 500 on the per-node plugin), selected by the sidecar a pod runs rather than by its name — ODF names those pods `openshift-storage.rbd.csi.ceph.com-ctrlplugin-*` while upstream uses `csi-rbdplugin-provisioner-*` |

The target namespace is read defensively (attribute, transport and parse failures all degrade to no snapshot) and the dump is skipped when the read fails or
`--skip-data-collector` is set, so collecting evidence can never replace the `MigrationPlanExecError` / `MigrationVmUnschedulableError` the caller is supposed to see.

Deliberately **not** dumped: the generic `MigrationPlanExecError`. Expected-failure tests (`tests/plan_lifecycle/`, `TestPostHookRetainFailedVm`, resume-conversion) raise it on
purpose, so paying for a snapshot there would only slow the suite down. Every failure path is a no-op under `--skip-data-collector`.

## Must-Gather Support

The suite has built-in must-gather support. `run_plan_must_gather()` matches a failure to a specific plan and runs a targeted gather; `run_must_gather()`
runs the full, cluster-wide gather. Both share image resolution and differ only in the arguments passed to the collector script.

```284:294:utilities/must_gather.py
        must_gather_image = _resolve_must_gather_image(
            ocp_admin_client=ocp_admin_client,
            mtv_subs=mtv_subs,
            mtv_csv=mtv_csv,
        )

        command = f"oc adm must-gather --image={must_gather_image} --dest-dir={data_collector_path}"
        if target_args:
            command = f"{command} -- {target_args}"
        success, _, _ = run_command(shlex.split(command), verify_stderr=False)
        return success
```

What this means in practice:

- Machine Config Operator state before the image gather is covered by the upgrade-scope failure snapshot above (`dump_cluster_state()`), which reads the same MCP, node and MCO
  pod evidence through the wrapper. No separate `oc adm inspect` dump runs any more.
- If the failure can be tied back to a specific plan, the gather is scoped to that plan with `/usr/bin/targeted`.
- Otherwise the gather runs unscoped and collects everything. That path is slow, so it is worth avoiding.
- `pytest_exception_interact` records a failing class item. A `tryfirst` `pytest_runtest_protocol` wrapper clears stale `plan_resource` on entry to a different class-plan identity before setup hooks run, but preserves it across consecutive methods of the same plan. The pre-finalizer boundary binds the current Plan or explicit None, even if no failure is pending yet, and retains it for retries. Updates before that boundary are observed. If setup fails before this plan exists, the gather is full, not targeted.
- Setup and call failures queue one gather per class-plan instance per worker. Ordinary methods with the same class-scoped parameter indices share state; different plan parameter sets and same-named classes in different modules do not.
- The `tryfirst` `pytest_runtest_teardown(item, nextitem)` hook gathers pending diagnostics before pytest's default fixture finalization when `nextitem` belongs to a different class-plan instance, or is `None`. This includes early `-x`/`--maxfail` exits and local boundaries under `--dist=load`, without waiting for the worker to execute the entire collected class.
- A plan-parameter transition is also a boundary even if pytest retains the same Class collector. Pytest can finalize the previous parametrized fixture tree during the next setup, so the gather must run before that setup.
- Deferral lets later methods contribute to the failed state before a single gather captures it. Collection precedes boundary cleanup, but resources explicitly deleted by earlier tests or fixtures may already be gone.
- A standalone test attempts a gather into its worker-qualified node directory on each failing phase until one succeeds. A failed attempt is not queued for session-finish retry; an unexpected setup/call collector error stays in that item's typed stash until its teardown wrapper yields to pytest fixture finalizers, then propagates with the original traceback. Reporting that deferred error does not retry collection or mark success. A collector error first raised after teardown propagates immediately because finalizers have already run.
- State lives on each worker's session object. Worker-qualified artifact names prevent workers from sharing a destination. At most one successful gather is recorded per class-plan instance per worker, including if scheduling later returns to that instance.
- Both gather entry points report success. Missing collector resources and known command, filesystem, authentication, API and transport failures return `False`. Those operational failures stay pending for session-finish retry only for class-plan gathers; failed standalone attempts are not queued. Missing or invalid CSV deployments/containers/environment values, IDMS mirror entries/sources, channels or image digest separators raise `MustGatherImageError` and propagate instead of returning `False`. The class-plan pending entry remains queued after that error, so a later boundary or session-finish retry may attempt the gather again and propagate the metadata error again. The wrapper has no dedicated missing-API exception; only its `_get_api_version` discovery error for the Subscription and CSV kinds is treated as operational — IDMS API-unavailability falls back to the CSV image inside image resolution. Malformed metadata structures and other programming or configuration errors also propagate with the class-plan entry left pending. `run_plan_must_gather()` takes the plan name and namespace; `run_must_gather()` is the full-collection path when no plan exists.
- A teardown failure on an earlier method is queued until the class-plan boundary when possible, so it does not prematurely mark the instance collected. A failure reported after boundary finalization gathers immediately only if no successful gather exists; it uses the bound pre-finalizer Plan context but necessarily sees post-finalizer cluster state. A successful pre-finalizer gather suppresses the repeat.
- The modern `wrapper=True` teardown hook yields in `finally` before propagating collector errors. Fixture finalizer exceptions propagate naturally without an old-style pluggy teardown warning, with any collector error chained. A class-plan boundary collector error is stashed on the boundary item, raised once as that item's teardown error, and consumed by the exception-interaction hook instead of being retried there — a second raise inside that hook would abort the worker with an INTERNALERROR. The pending entry stays queued for the session-end flush, so a later boundary or session-finish retry may surface the error again. The session-finish wrapper runs the pending-gather flush, resource inventory writing, configured teardown and temporary-directory removal before it yields to pytest session hooks — the xdist worker reports completion from its own session-finish wrapper, and cleanup placed after that yield could be interrupted by worker shutdown mid-deletion — then runs the extra summary and enabled AI enrichment, which need other plugins' session-finish output. A cleanup error raised before the yield is stashed and re-raised after it, so later session-hook or cleanup failures take precedence with earlier errors chained, not silently discarded.
- This guarantees independent cleanup attempts after collector, inventory or teardown errors, not successful cleanup. The existing inventory helper logs write failures. Session teardown failures are logged, trigger the configured full gather, and re-raise; if that gather raises too, its error propagates with the teardown error chained. Process crashes and forced termination remain outside this guarantee.
- Session finish retries exceptional remaining pending gathers on a surviving worker using the bound context, but cannot promise pre-cleanup resources after interrupted hooks. If a boundary never bound the context, fallback reads the class attribute only for the still-active identity; otherwise it binds None and gathers fully rather than targeting another plan. It cannot recover in-memory pending state from a crashed worker. Artifacts are not guaranteed if retries fail.
- If teardown leaves leftovers behind, an unscoped `run_must_gather()` runs at session finish as a fallback.

> **Note:** The must-gather image is not hardcoded. The code resolves it from the installed MTV operator CSV, so the
> gather matches the cluster’s installed MTV build. The channel-derived `ImageDigestMirrorSet` is an optional
> accelerator: when it is absent (GA/brew clusters) or its API is unavailable (older OpenShift), the fully-qualified
> CSV `MUST_GATHER_IMAGE` is used verbatim — any cluster with direct registry access can pull it. Only its mirror
> URL ever rewrites the registry prefix; the image itself is always the MTV one.
>
> **Note:** Only known operational collection failures are logged and returned as `False`. Unexpected programming and configuration errors remain visible.
>
> **Tip:** For a shared fixture failure, expect one directory per class-plan instance per worker, normally collected at the pre-finalizer boundary. Session-end retries and failures first reported by boundary finalizers may instead describe cleaned state.

## Common Failure Points

Most failures in this repo fall into one of these buckets.

### Before Migration Starts

- Missing required config such as `storage_class` or `source_provider`. `pytest_sessionstart` treats exactly these two as required and
  calls `pytest.exit` with code 1 when either is absent.
- Missing or empty `.providers.json`.
- A provider key passed with `--tc=source_provider:...` that does not exist in `.providers.json`.
- Wrong cluster credentials from `cluster_host`, `cluster_username`, or `cluster_password`.
- SSL verification mismatches between your config and the created provider secret. Note the config default is `insecure_verify_skip: str = "true"`.
- `forklift-*` pods not being healthy before tests begin.

These failures usually show up in fixtures or session startup, before you ever get a `Migration` CR.

That required-config check is skipped under `--collect-only` and `--setup-plan`, which is why the fast wiring checks can run without a configured
cluster at all.

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
- A migrated VM stays unschedulable on the destination cluster (for example a `target_node_selector` pinning it to one worker
  that lacks memory). `wait_for_migration_complate()` fails fast here with `MigrationVmUnschedulableError` after the destination
  `VirtualMachineInstance` reports `PodScheduled=False`/`Unschedulable` for 3 consecutive 30-second checks, instead of waiting for
  `plan_wait_timeout`. The suite never frees node capacity, so this state does not recover within a run.
- Hook execution fails at `PreHook` or `PostHook`.
- Warm migration timing is wrong for the environment.

### Starting a Migrated VM

`OCPProvider.vm_dict()` starts the destination VM and waits for it to run before any post-migration check reads its
power state. Both waits are bounded and sample the VM's `printableStatus`, because the intermediate statuses are normal
and not failures:

- The power-state read waits up to 120 seconds for `Running` or `Stopped` before recording `power_state`.
- The start verdict waits up to 300 seconds for `Running`. `Starting` with the condition
  `Ready: GuestNotRunning` only means the guest has not booted yet — the wrapper's `status.ready` turns true before
  `printableStatus` reaches `Running`, so a single sample is not a valid verdict. A VM still in `Starting` when the
  300-second wait expires does raise `ValueError`; the state is tolerated, not ignored.
- Only a confirmed `Stopped` reading is recorded as `power_state="off"` — that value drives the restore-to-off path, so
  an unresolved pre-start status is left unset and becomes `on` once the start wait confirms `Running`.
- The "did not reach status" `ValueError` reports the last observed status together with the VM conditions, so a
  genuine start failure names the state the VM ended in instead of the first sample.

The 300-second bound matches the explicit `timeout=300` the copy-offload tests already use for slow-booting multi-disk
VMs. If a VM legitimately needs longer, raise the timeout rather than reintroducing a single-sample check.

The repo’s default timeout config is important here:

- `plan_wait_timeout` defaults to `3600` seconds.
- `mins_before_cutover` defaults to `5`.
- `mtv_namespace` defaults to `openshift-mtv`.

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

> **Tip:** If the test only fails in `test_check_vms`, the migration itself may already be complete. At that point, spend less time in the initial `Plan` conditions and more time
> on the migrated VM, its PVCs, its launcher/VMI state, and guest-level checks.

## Copy-Offload-Specific Checks

Copy-offload adds extra prerequisites, so it also adds extra failure modes.

```30:71:.providers.json.example
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

      # ... the optional "resource_pool" guidance block is elided here ...

      "storage_hostname": "storage.example.com",
      "storage_username": "admin",
      "storage_password": "your-password-here",  # pragma: allowlist secret
```

The same block also accepts an optional `esxi_clone_method` of `"vib"` (the default) or `"ssh"` with matching `esxi_host`, `esxi_user`, and
`esxi_password`, plus an optional `rdm_lun_uuid` for RDM disk tests. The ESXi values can also be overridden through the `COPYOFFLOAD_ESXI_HOST`,
`COPYOFFLOAD_ESXI_USER`, and `COPYOFFLOAD_ESXI_PASSWORD` environment variables.

Check these first for copy-offload failures:

- `storage_vendor_product` is correct for your storage backend.
- `datastore_id` is valid and matches where the source VM disks actually live.
- `secondary_datastore_id` and `non_xcopy_datastore_id` are only used when the test really needs them.
- Storage credentials are present either in `.providers.json` or the matching `COPYOFFLOAD_*` environment variables.
- Vendor-specific fields are set when your selected storage vendor requires them.
- If you use SSH cloning, the ESXi host, user, and password are correct.
- If the offload path itself is the problem, inspect the `forklift-volume-populator-controller` logs in `openshift-mtv` in addition to the normal MTV controller logs.

> **Warning:** Copy-offload failures are often configuration issues first, product issues second. Always verify the storage backend, datastore IDs, and vendor-specific fields
> before assuming the migration code is at fault.

## Expected Failures and Hook Tests

Not every `MigrationPlanExecError` means something is wrong. This repo contains tests that intentionally expect migration failure and then validate how that failure happened.

A good example is `test_post_hook_retain_failed_vm`, which expects the migration to fail and then checks whether the failure happened at the expected hook step:

```199:214:tests/hooks/test_post_hook_retain_failed_vm.py
expected_result = prepared_plan["expected_migration_result"]

if expected_result == "fail":
    with pytest.raises(MigrationPlanExecError) as exc_info:
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )
    try:
        self.__class__.should_check_vms = validate_hook_failure_and_check_vms(self.plan_resource, prepared_plan)
    except Exception as e:
        # Chain with original migration error so the root cause is visible in traceback
        e.__cause__ = exc_info.value
        raise
```

Why this matters:

- Some tests deliberately expect `PreHook` or `PostHook` failure.
- A `PostHook` failure can still leave a migrated VM behind, and that VM is worth inspecting.
- The real question is often not “did it fail?” but “did it fail at the expected step?”

> **Tip:** In class-based incremental tests, focus on the first real failure. Later tests in the same class may be marked `xfail` because
> the earlier step already failed. `conftest.py` implements this by recording the first failed `incremental`-marked test and xfail-ing every
> later test in the same class.

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
| `forklift-volume-populator-controller` logs | Populator scheduling and throttling for volume copy | Copy-offload and long-transfer stalls |
| Target namespace events and pods | `virt-v2v`, DV/PVC/PV, VM/VMI, launcher behavior | Transfer, boot, and runtime issues |
| Source provider logs | vCenter, RHV, or OpenStack-side errors | External provider problems |

A practical first command set is:

- `oc logs -n openshift-mtv deployment/forklift-controller`
- `oc logs -n openshift-mtv deployment/forklift-volume-populator-controller`
- `oc get migration <name> -n <namespace> -o yaml`
- `oc get plan <name> -n <namespace> -o yaml`
- `oc get provider <name> -n <namespace> -o yaml`
- `oc get vm <name> -n <namespace> -o yaml`
- `oc get events -n <namespace> --sort-by='.lastTimestamp'`

`openshift-mtv` is the default `mtv_namespace`; pass `--tc=mtv_namespace:<ns>` to target a different operator namespace.

## A Good Debugging Order

If you want one repeatable process, use this:

1. Open `pytest-tests.log` and identify the first failing test and phase.
2. Check whether the failure is an expected one, especially in hook tests or incremental classes.
3. Read `junit-report.xml` for the same test case and capture the resource names involved.
4. Open `.data-collector/resources.json` and any must-gather output under `.data-collector/`.
5. Inspect the `Plan`, `Migration`, and `Provider` CRs.
6. Check `forklift-controller`, `forklift-volume-populator-controller`, and target-namespace pod logs.
7. If the migration succeeded but the test still failed, inspect the migrated VM, its PVCs, its VMI/launcher state, and guest-level connectivity instead of only looking at the
   controller.

That order lines up with how this repository itself reports and classifies failures, and it usually gets you to the root cause faster than starting from cluster logs alone.
