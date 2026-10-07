# Pytest Options And Markers

This suite comes with an opinionated `pytest` setup. If you run `pytest` or `uv run pytest` from the repository root, it already knows where tests live, which config file to load,
how to write JUnit XML, and how to behave when you enable xdist.

> **Warning:** A real test run requires `source_provider` and `storage_class`. The session start hook exits early if either is missing. In practice, you usually pass them with
> `--tc=...`.

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

## Default pytest behavior

The repo-level defaults live in `pytest.ini`:

```ini
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

That is 21 registered markers. Because `--strict-markers` is on, any `@pytest.mark.<name>` that is not listed above is a collection-time error, not a warning.

What that means in day-to-day use:

- `testpaths = tests` limits default discovery to the `tests/` directory.
- `-s` disables output capture, so test output is shown live.
- `-p no:logging` disables pytest’s built-in logging plugin. The suite sets up its own console and file logging in `conftest.py`.
- `--tc-file=tests/tests_config/config.py` and `--tc-format=python` load defaults through `pytest-testconfig`. The same plugin provides the per-run `--tc=key:value`
  overrides used above.
- `--junit-xml=junit-report.xml` always writes a JUnit report in the repo working directory.
- `junit_logging = all` means logs are included in the JUnit output.
- `--show-progress` enables progress output from `pytest-progress`.
- `--strict-markers` turns marker typos into immediate errors instead of silently ignoring them.
- `--dist=loadscope` preconfigures xdist scheduling, but it does not start parallel workers by itself. You still need to add `-n` if you want xdist.

`pytest_configure` also sets a unique `basetemp` per session when you did not pass one:

```python
def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp is None:
        config.option.basetemp = str(Path(tempfile.gettempdir()) / f"pytest-{uuid.uuid4().hex[:8]}")
```

`--basetemp=/path` still wins. The auto-generated directory is removed again at session finish (`shutil.rmtree`) on real runs, so nothing is left behind in `/tmp`.

The suite also rewrites collected item names so reports are easier to read:

```python
for item in items:
    item.name = f"{item.name}-{py_config.get('source_provider')}-{py_config.get('storage_class')}"
```

> **Note:** Because collected names are rewritten, terminal output and JUnit entries include the selected `source_provider` and `storage_class`, not just the raw test function
> name. The rewrite runs `tryfirst` in `pytest_collection_modifyitems`, so it happens before pytest deselects by keyword or marker. `-k` still matches class names and parametrize
> ids because pytest matches against parent node names as well.

## Marker reference

`pytest.ini` registers 21 markers. The descriptions below are taken verbatim from that file.

| Marker | Registered description | Kind |
| --- | --- | --- |
| `tier0` | Core functionality tests (smoke tests) | Tier |
| `tier1` | Extended functionality tests | Tier |
| `remote` | Remote cluster migration tests | Scenario |
| `warm` | Warm migration tests | Scenario |
| `copyoffload` | Copy-offload (XCOPY) tests | Scenario |
| `copyoffload_sanity` | Copy-offload sanity tests - core scenarios for quick validation | Scenario |
| `copyoffload_snapshots` | Copy-offload snapshot tests (vSphere only) | Scenario |
| `shared_disk` | Shared disk migration tests (vSphere only) | Scenario |
| `incremental` | Marks tests as incremental (xfail on previous failure) | Lifecycle |
| `min_mtv_version` | Mark test to require minimum MTV version (e.g., `@pytest.mark.min_mtv_version("2.6.0")`) | Lifecycle |
| `ova` | OVA provider-specific tests | Provider |
| `vsphere` | vSphere provider-specific tests | Provider |
| `esxi` | ESXi provider-specific tests | Provider |
| `rhv` | RHV provider-specific tests | Provider |
| `hyperv` | Hyper-V provider-specific tests | Provider |
| `openstack` | OpenStack provider-specific tests | Provider |
| `openshift` | OpenShift provider-specific tests | Provider |
| `deep_inspection` | Deep Inspection / Conversion CR tests (vSphere only) | Integration |
| `ca_crt` | CA certificate field (ca.crt) provider secret tests | Integration |
| `aap` | AAP (Ansible Automation Platform) hook integration tests (vSphere only) | Integration |
| `upgrade` | MTV operator upgrade tests | Integration |

### Which files apply each marker

Paths are relative to `tests/`.

Tier markers:

- `tier0`:
  `cold/test_cold_migration_comprehensive.py`, `cold/test_mtv_cold_migration.py`, `cold/test_ova_cold_migration.py`,
  `hooks/test_post_hook_retain_failed_vm.py`, `shared_disk/test_shared_disk_rhel_migration.py`,
  `warm/test_mtv_warm_clusterrole_migration.py`, `warm/test_mtv_warm_migration.py`,
  `warm/test_warm_migration_comprehensive.py`
- `tier1`:
  `cold/test_ca_crt_migration.py`, `cold/test_cold_migration_xfs.py`, `cold/test_dual_nic_cold_migration.py`,
  `cold/test_insecure_skip_verify_migration.py`, `deep_inspection/test_plan_driven_di.py`,
  `deep_inspection/test_standalone_di.py`, `hooks/test_aap_hook_migration.py`, `luks/test_luks_cold_migration.py`,
  `plan_lifecycle/test_plan_archive_pvc_cleanup.py`, `shared_disk/test_shared_disk_windows_migration.py`,
  `warm/test_mtv_warm_clusterrole_migration.py`, `warm/test_warm_migration_xfs.py`

Scenario markers:

- `remote`:
  `cold/test_mtv_cold_migration.py`, `warm/test_mtv_warm_clusterrole_migration.py`, `warm/test_mtv_warm_migration.py`
- `warm`:
  `copyoffload/test_copyoffload_migration.py`, `deep_inspection/test_plan_driven_di.py`,
  `warm/test_mtv_warm_clusterrole_migration.py`, `warm/test_mtv_warm_migration.py`,
  `warm/test_warm_migration_comprehensive.py`, `warm/test_warm_migration_xfs.py`
- `copyoffload`, `copyoffload_sanity`, `copyoffload_snapshots`:
  `copyoffload/test_copyoffload_migration.py` only
- `shared_disk`:
  `shared_disk/test_shared_disk_rhel_migration.py`, `shared_disk/test_shared_disk_windows_migration.py`

Lifecycle markers:

- `incremental`: applied in all 21 test modules of the suite, in practice on every multi-step class
- `min_mtv_version`: no test in the repo uses it yet; the `mtv_version_checker` fixture in `conftest.py` implements it

Provider markers:

- `vsphere`:
  every test module except `cold/test_ova_cold_migration.py`
- `esxi`: `cold/test_ca_crt_migration.py`, `cold/test_insecure_skip_verify_migration.py`, `cold/test_mtv_cold_migration.py`
- `rhv`:
  `cold/test_ca_crt_migration.py`, `cold/test_cold_migration_comprehensive.py`, `cold/test_insecure_skip_verify_migration.py`,
  `cold/test_mtv_cold_migration.py`, `hooks/test_post_hook_retain_failed_vm.py`,
  `plan_lifecycle/test_plan_archive_pvc_cleanup.py`, `warm/test_mtv_warm_migration.py`,
  `warm/test_warm_migration_comprehensive.py`
- `hyperv`: `cold/test_cold_migration_comprehensive.py`, `cold/test_mtv_cold_migration.py`
- `openstack`:
  `cold/test_ca_crt_migration.py`, `cold/test_cold_migration_comprehensive.py`,
  `cold/test_insecure_skip_verify_migration.py`, `cold/test_mtv_cold_migration.py`,
  `hooks/test_post_hook_retain_failed_vm.py`, `plan_lifecycle/test_plan_archive_pvc_cleanup.py`
- `openshift`:
  `cold/test_cold_migration_comprehensive.py`, `cold/test_mtv_cold_migration.py`,
  `hooks/test_post_hook_retain_failed_vm.py`, `plan_lifecycle/test_plan_archive_pvc_cleanup.py`
- `ova`: `cold/test_ova_cold_migration.py`

Integration markers:

- `deep_inspection`: `deep_inspection/test_plan_driven_di.py`, `deep_inspection/test_standalone_di.py`
- `ca_crt`: `cold/test_ca_crt_migration.py`
- `aap`: `hooks/test_aap_hook_migration.py`
- `upgrade`: `upgrade/test_upgrade_migration.py`, `warm/test_mtv_warm_migration.py`

Provider markers are documentation, not enforcement: a class tagged `rhv` still runs if you point `source_provider` at something else. Only the collection-time rules further down
actually skip anything.

### Copy-offload sub-markers in detail

Inside `tests/copyoffload/test_copyoffload_migration.py` the sub-markers select smaller slices of the same file:

- `copyoffload_sanity`:
  `TestCopyoffloadThickEagerSnapshotsMigration`, `TestCopyoffloadRdmVirtualDiskMigration`,
  `TestCopyoffloadMultiDatastoreMigration`, `TestCopyoffloadMixedDatastoreMigration`,
  `TestCopyoffload10MixedDisksMigration`, `TestCopyoffloadWarmMigration`, `TestCopyoffloadScaleMigration`
- `copyoffload_snapshots`:
  `TestCopyoffloadThickLazySnapshotsMigration`, `TestCopyoffloadThickEagerSnapshotsMigration`,
  `TestCopyoffloadThinSnapshotsMigration`, `TestCopyoffload2TbVmSnapshotsMigration`
- `warm`: `TestCopyoffloadWarmRdmVirtualDiskMigration`, `TestCopyoffloadWarmMigration`

### Marker combinations in real classes

A typical warm class stacks several markers at once:

```python
@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.upgrade
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_sanity_warm_mtv_migration"],
        )
    ],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestSanityWarmMtvMigration:
```

A remote-only class adds `remote` plus an explicit config gate:

```python
@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.warm
@pytest.mark.remote
@pytest.mark.incremental
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
```

The repo also supports version-gated tests through `min_mtv_version`, but the marker only has effect when the checker fixture is active:

```python
@pytest.mark.usefixtures("mtv_version_checker")
@pytest.mark.min_mtv_version("2.10.0")
def test_something(...):
    # Test runs only if MTV >= 2.10.0
```

## Collection-time provider skip rules

`pytest_collection_modifyitems` in `conftest.py` reads `.providers.json`, resolves the configured `source_provider`, and adds skip markers before any test runs. All of these rules
are skipped entirely in dry-run mode.

| Source provider type | Markers that get skipped | Skip reason emitted |
| --- | --- | --- |
| `openstack`, `openshift`, `ova`, `hyperv` | `warm` | `<type> warm migration is not supported.` |
| Anything other than `vsphere` | `copyoffload`, `shared_disk`, `deep_inspection`, `aap`, and the `luks` keyword | `Test is only applicable to vSphere source providers` |
| `openshift`, `ova` | `ca_crt` | `<type> does not use CA certificates in provider secret` |

> **Note:** The `luks` keyword in that second rule is defensive only. No test in the repo registers a `luks` marker, and `luks` is not a registered marker in `pytest.ini`, so the
> keyword currently matches nothing.

Because these rules live in a hook rather than in module-level `skipif` markers, `-m warm` on an unsupported provider still collects the tests and reports them as skipped
instead of deselecting them.

## Selection and dry-run modes

This suite supports the standard pytest selection tools, and they map cleanly to how the tests are organized.

### Marker selection with `-m`

Use project markers to slice the suite by scenario type. `--strict-markers` only validates marks applied to tests, not `-m` expressions; an unknown name in `-m` silently
selects no tests, so double-check the spelling against the table above.

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

In the same way, you can select other registered markers such as `tier0`, `tier1`, `warm`, `remote`, `copyoffload_sanity`, `copyoffload_snapshots`, `shared_disk`, or `upgrade`, and
combine them with pytest’s `or` / `and` syntax:

```bash
uv run pytest -m "tier0 or warm" -v --tc=source_provider:vsphere-8.0.3.00400 --tc=storage_class:my-block-storageclass
```

### Keyword selection with `-k`

Copy-offload tests are class-based and parameterized with stable Jira-style ids, so `-k` works best on the class name or the id slug:

```bash
# Whole class
uv run pytest -m copyoffload -k TestCopyoffloadThinMigration ...

# Slug from the parametrize id (ids=["MTV-559:copyoffload-thin"])
uv run pytest -m copyoffload -k "copyoffload-thin" ...
```

| Class | Parametrize id |
| --- | --- |
| `TestCopyoffloadThinMigration` | `MTV-559:copyoffload-thin` |
| `TestCopyoffloadThickLazyMigration` | `MTV-580:copyoffload-thick-lazy` |
| `TestCopyoffloadThickEagerMigration` | `MTV-582:copyoffload-thick-eager` |
| `TestCopyoffloadMultiDiskMigration` | `MTV-561:copyoffload-multi-disk` |
| `TestCopyoffloadMultiDiskDifferentPathMigration` | `MTV-563:copyoffload-multi-disk-different-path` |
| `TestCopyoffloadRdmVirtualDiskMigration` | `MTV-562:copyoffload-rdm-virtual` |
| `TestCopyoffloadMultiDatastoreMigration` | `MTV-564:copyoffload-multi-datastore` |
| `TestCopyoffloadWarmMigration` | `MTV-577:copyoffload-warm` |

> **Note:** The keys in `tests/tests_config/config.py` (for example `test_copyoffload_thin_migration`) are pytest config keys, not test node names, so
> `-k test_copyoffload_thin_migration` matches nothing.

### Standard pytest path and node selection

This repo does not replace pytest’s normal file, class, or node selection. Tests live in one directory per scenario area, so selecting by file works as expected:

```bash
uv run pytest tests/copyoffload/test_copyoffload_migration.py -v --tc=source_provider:vsphere-8.0.3.00400
uv run pytest tests/warm/test_mtv_warm_migration.py::TestSanityWarmMtvMigration -v
```

### Supported dry-run modes

The suite explicitly treats `--collect-only` and `--setup-plan` as dry-run modes:

```python
def is_dry_run(config: pytest.Config) -> bool:
    """Check if pytest was invoked in dry-run mode (collectonly or setupplan)."""
    return config.option.setupplan or config.option.collectonly
```

Those dry-run modes are used in repository automation too:

```toml
commands = [
  [
    "uv",
    "run",
    "pytest",
    "--setup-plan",
  ],
  [
    "uv",
    "run",
    "pytest",
    "--collect-only",
  ],
]
```

The container image also defaults to dry-run discovery:

```dockerfile
CMD ["uv", "run", "pytest", "--collect-only"]
```

`--collect-only` is the safest way to preview what your `-m` and `-k` expression will match:

```bash
pytest --collect-only -m copyoffload
```

`--setup-plan` is useful when you want pytest to show fixture setup planning without running the tests themselves.

> **Note:** In this repository, dry-run mode is more than “just don’t execute tests.” When `--collect-only` or `--setup-plan` is active, the suite skips runtime-only
> validation, teardown, failure data collection, must-gather capture, and AI failure analysis.

Dry-run still validates that your selection expression and configuration are syntactically usable:

> **Warning:** Dry-run does not validate a migration path. It only validates collection and, for `--setup-plan`, setup planning. It does not exercise MTV, providers, or
> cluster-side migration behavior.

## Incremental semantics

Most test classes in this repository are structured as a multi-step workflow: create the `StorageMap`, create the `NetworkMap`, create the `Plan`, execute the migration, then
validate the migrated VMs.

That is why `incremental` matters so much here. The suite implements its own incremental behavior in `conftest.py`:

```python
# Incremental test support - track failures for class-based tests
if "incremental" in item.keywords and rep.when == "call" and rep.failed:
    item.parent._previousfailed = item
```

```python
# Incremental test support - xfail if previous test in class failed
if "incremental" in item.keywords:
    previousfailed = getattr(item.parent, "_previousfailed", None)
    if previousfailed is not None:
        pytest.xfail(f"previous test failed ({previousfailed.name})")
```

In practice, that means:

- The first real failure in an incremental class is the one you should focus on.
- Later tests in the same class are converted to `xfail` with a message like `previous test failed (...)`.
- This prevents a broken early step from creating a long tail of noisy follow-up failures.

There is one important nuance: this implementation only records failures from the `call` phase. A setup or teardown error does not set `_previousfailed` the same way a call-phase
failure does.

> **Tip:** When an incremental class fails, start with the earliest failing step in the class. Later `xfail` results are usually downstream effects, not new root causes.

## xdist behavior

`pytest-xdist` is installed and the suite is xdist-aware, but parallel execution is opt-in. Nothing in `pytest.ini` sets a worker count, so runs stay single-process until you add
`-n`.

What is preconfigured is the distribution strategy:

- `--dist=loadscope` is enabled by default.
- That is a good fit for this repository because tests are heavily class-based, use shared class attributes, and often rely on `incremental` semantics.
- Keeping related tests together on one worker reduces the chance of splitting a multi-step class across workers.

`pytest-harvest` needs explicit xdist plumbing, and the suite provides all four hooks. The worker-side dump persists each worker’s session state to disk:

```python
def pytest_harvest_xdist_worker_dump(
    worker_id: str,
    session_items: list[Any],
    fixture_store: dict[str, Any],
) -> None:
    """Persist this xdist worker's harvested results so the controller can merge them.

    Args:
        worker_id: Identifier of the xdist worker
        session_items: Harvested test items collected on this worker
        fixture_store: Fixture store captured on this worker
    """
    # persist session_items and fixture_store in the file system
    with open(RESULTS_PATH / (f"{worker_id}.pkl"), "wb") as f:
        try:
            pickle.dump((session_items, fixture_store), f)
        except Exception as exp:
            LOGGER.warning(f"Error while pickling worker {worker_id}'s harvested results: [{exp.__class__}] {exp}")
```

Those pickles land in `./.xdist_results/` (`RESULTS_PATH`), which is wiped and recreated by `pytest_harvest_xdist_init` and removed again by `pytest_harvest_xdist_cleanup`.

And it protects worker-shared setup where needed. For example, the `virtctl_binary` fixture uses a file lock and a shared cache directory specifically for xdist-safe downloads.

> **Tip:** If you enable parallelism with `-n`, keep the default `--dist=loadscope`. It matches the suite’s class-based design much better than fine-grained distribution.

## Suite-specific pytest options

`conftest.py` registers six custom options across five groups. All of them are real flags you can pass on any run.

```python
def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the suite's custom pytest options in their groups.

    Args:
        parser: Pytest command line parser
    """
    data_collector_group = parser.getgroup(name="DataCollector")
    teardown_group = parser.getgroup(name="Teardown")
    openshift_python_wrapper_group = parser.getgroup(name="Openshift Python Wrapper")
    analyze_with_ai_group = parser.getgroup(name="Analyze with AI")
    analyze_with_ai_group.addoption("--analyze-with-ai", action="store_true", help="Analyze test failures using AI")

    providers_group = parser.getgroup(name="Providers")
    providers_group.addoption(
        "--providers-json",
        help="Path to providers JSON configuration file. Falls back to PROVIDERS_JSON_PATH env var, then .providers.json",
        default=None,
    )

    data_collector_group.addoption("--skip-data-collector", action="store_true", help="Collect data for failed tests")
    data_collector_group.addoption(
        "--data-collector-path", help="Path to store collected data for failed tests", default=".data-collector"
    )
    teardown_group.addoption(
        "--skip-teardown", action="store_true", help="Do not teardown resource created by the tests"
    )
    openshift_python_wrapper_group.addoption(
        "--openshift-python-wrapper-log-debug",
        action="store_true",
        help="Enable debug logging in the openshift-python-wrapper module",
    )
```

| Option | Group | Behavior |
| --- | --- | --- |
| `--analyze-with-ai` | `Analyze with AI` | Enriches the JUnit XML after a failing run. Disabled automatically when `ROOTCOZ_SERVER_URL` is unset or in dry-run mode |
| `--providers-json PATH` | `Providers` | Path to the providers file. Falls back to `PROVIDERS_JSON_PATH`, then `.providers.json`; a missing file raises `FileNotFoundError` |
| `--skip-data-collector` | `DataCollector` | Skips failure data collection: no `resources.json` dump and no must-gather capture |
| `--data-collector-path PATH` | `DataCollector` | Output directory; default `.data-collector`. Wiped at session start, written at session finish |
| `--skip-teardown` | `Teardown` | Preserves resources after the run instead of deleting them |
| `--openshift-python-wrapper-log-debug` | `Openshift Python Wrapper` | Sets `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG` during session startup |

The real-run guard for required config lives in `pytest_sessionstart` too:

```python
required_config = ("storage_class", "source_provider")

if not is_dry_run(session.config):
    missing_configs: list[str] = []

    for _req in required_config:
        if not py_config.get(_req):
            missing_configs.append(_req)

    if missing_configs:
        pytest.exit(reason=f"Some required config is missing {required_config=} - {missing_configs=}", returncode=1)
```

And teardown/data collection behavior is handled in `pytest_sessionfinish`:

```python
if not session.config.getoption("skip_data_collector"):
    collect_created_resources(session_store=_session_store, data_collector_path=_data_collector_path)

if session.config.getoption("skip_teardown"):
    LOGGER.warning("User requested to skip teardown of resources")

else:
    try:
        session_teardown(session_store=_session_store)
    except Exception as exp:
        LOGGER.error(f"the following resources was left after tests are finished: {exp}")
        if not session.config.getoption("skip_data_collector"):
            run_must_gather(data_collector_path=_data_collector_path)
```

> **Warning:** `--skip-teardown` is a debugging tool, not a normal operating mode. If you use it, expect to clean up VMs, Plans, Providers, namespaces, and any source-side cloned
> resources yourself.

The `--skip-data-collector` help text is misleading. The implementation uses it as a true skip flag: when it is set, the suite neither writes `resources.json` nor runs
must-gather on failures.

One more dependency deserves a warning, since it is easy to assume otherwise from the old configuration files you may have seen:

> **Note:** `pytest-jira` is installed as a dependency and the repo ships a `jira.cfg.example` template, but `--jira` is **not** part of `addopts` and no test uses
> `pytest.mark.jira`. If you want JIRA markers active, add `--jira` yourself and supply `jira.cfg`.

Finally, when you are assembling a complex selection expression:

> **Tip:** Use `--collect-only` first. Once the collected set looks right, rerun the same command without dry-run mode.
