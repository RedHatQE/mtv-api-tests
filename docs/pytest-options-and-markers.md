# Pytest Options And Markers

This suite comes with an opinionated `pytest` setup. If you run `pytest` or `uv run pytest` from the repository root, it already knows where tests live, which config file to load, how to write JUnit XML, and how to behave when you enable xdist.

> **Warning:** A real test run requires `source_provider` and `storage_class`. The session start hook exits early if either is missing. In practice, you usually pass them with `--tc=...`.

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

What that means in day-to-day use:

- `testpaths = tests` limits default discovery to the `tests/` directory.
- `-s` disables output capture, so test output is shown live.
- `-p no:logging` disables pytest’s built-in logging plugin. The suite sets up its own console and file logging in `conftest.py`.
- `--tc-file=tests/tests_config/config.py` and `--tc-format=python` load defaults through `pytest-testconfig`.
- `--junit-xml=junit-report.xml` always writes a JUnit report in the repo working directory.
- `junit_logging = all` means logs are included in the JUnit output.
- `--basetemp=/tmp/pytest` gives pytest a fixed temp root for the run.
- `--show-progress` enables progress output from `pytest-progress`.
- `--strict-markers` turns marker typos into immediate errors instead of silently ignoring them.
- `--jira` enables `pytest-jira`.
- `--dist=loadscope` preconfigures xdist scheduling, but it does not start parallel workers by itself. You still need to add `-n` if you want xdist.

The suite also rewrites collected item names so reports are easier to read:

```python
for item in items:
    item.name = f"{item.name}-{py_config.get('source_provider')}-{py_config.get('storage_class')}"
```

> **Note:** Because collected names are rewritten, terminal output and JUnit entries include the selected `source_provider` and `storage_class`, not just the raw test function name.

## Marker reference

The suite registers these project markers:

| Marker | What it means | Typical use in this repo |
| --- | --- | --- |
| `tier0` | Core smoke and sanity coverage | Basic cold/warm migration flows and comprehensive smoke-style scenarios |
| `warm` | Warm migration scenarios | Warm migration tests, including one copy-offload warm case |
| `remote` | Remote OpenShift destination scenarios | Tests that require `remote_ocp_cluster` to be configured |
| `copyoffload` | Copy-offload/XCOPY scenarios | The large copy-offload suite in `tests/test_copyoffload_migration.py` |
| `incremental` | Sequential class semantics | Multi-step migration classes where later steps depend on earlier ones |
| `min_mtv_version` | MTV version gate | Used with `mtv_version_checker` to skip below a required MTV version |

A typical class combines multiple markers and environment gates:

```python
pytestmark = [
    pytest.mark.skipif(
        _SOURCE_PROVIDER_TYPE
        in (Provider.ProviderType.OPENSTACK, Provider.ProviderType.OPENSHIFT, Provider.ProviderType.OVA),
        reason=f"{_SOURCE_PROVIDER_TYPE} warm migration is not supported.",
    ),
]

if _SOURCE_PROVIDER_TYPE == Provider.ProviderType.RHV:
    pytestmark.append(pytest.mark.jira("MTV-2846", run=False))

@pytest.mark.tier0
@pytest.mark.warm
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

A remote-only class is gated explicitly:

```python
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

> **Note:** Some warm tests add `pytest.mark.jira("MTV-2846", run=False)` for RHV. That behavior comes from `pytest-jira`, which is enabled by default via `--jira`. The repo includes a `jira.cfg.example` template for that plugin.

## Selection and dry-run modes

This suite supports the standard pytest selection tools, and they map cleanly to how the tests are organized.

### Marker selection with `-m`

Use project markers to slice the suite by scenario type. The checked-in docs already use this pattern:

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

In the same way, you can select other registered markers such as `tier0`, `warm`, or `remote`.

### Keyword selection with `-k`

`-k` works well because test names are descriptive. The repository’s own docs call this out directly:

- Add `-k test_name` after `-m copyoffload`.
- Example: `-m copyoffload -k test_copyoffload_thin_migration`

The repo also lists concrete test names you can target with `-k`:

- `test_copyoffload_thin_migration`
- `test_copyoffload_thick_lazy_migration`
- `test_copyoffload_multi_disk_migration`
- `test_copyoffload_multi_disk_different_path_migration`
- `test_copyoffload_rdm_virtual_disk_migration`

### Standard pytest path and node selection

This repo does not replace pytest’s normal file, class, or node selection. If you prefer selecting by file or specific test node, standard pytest syntax still applies.

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

`--collect-only` is the safest way to preview what your `-m` and `-k` expression will match. The checked-in copy-offload docs recommend it directly:

```bash
pytest --collect-only -m copyoffload
```

`--setup-plan` is useful when you want pytest to show fixture setup planning without running the tests themselves.

> **Note:** In this repository, dry-run mode is more than “just don’t execute tests.” When `--collect-only` or `--setup-plan` is active, the suite skips runtime-only validation, teardown, failure data collection, must-gather capture, and AI failure analysis.

> **Warning:** Dry-run does not validate a migration path. It only validates collection and, for `--setup-plan`, setup planning. It does not exercise MTV, providers, or cluster-side migration behavior.

## Incremental semantics

Most test classes in this repository are structured as a five-step workflow:

1. Create `StorageMap`
2. Create `NetworkMap`
3. Create `Plan`
4. Execute migration
5. Validate migrated VMs

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

There is one important nuance: this implementation only records failures from the `call` phase. A setup or teardown error does not set `_previousfailed` the same way a call-phase failure does.

> **Tip:** When an incremental class fails, start with the earliest failing step in the class. Later `xfail` results are usually downstream effects, not new root causes.

## xdist behavior

`pytest-xdist` is installed and the suite is xdist-aware, but parallel execution is opt-in. Nothing in `pytest.ini` sets a worker count, so runs stay single-process until you add `-n`.

What is preconfigured is the distribution strategy:

- `--dist=loadscope` is enabled by default.
- That is a good fit for this repository because tests are heavily class-based, use shared class attributes, and often rely on `incremental` semantics.
- Keeping related tests together on one worker reduces the chance of splitting a multi-step class across workers.

The suite also includes explicit worker-side handling for `pytest-harvest` state:

```python
def pytest_harvest_xdist_worker_dump(worker_id, session_items, fixture_store):
    # persist session_items and fixture_store in the file system
    with open(RESULTS_PATH / (f"{worker_id}.pkl"), "wb") as f:
        try:
            pickle.dump((session_items, fixture_store), f)
        except Exception as exp:
            LOGGER.warning(f"Error while pickling worker {worker_id}'s harvested results: [{exp.__class__}] {exp}")
```

And it protects worker-shared setup where needed. For example, the `virtctl_binary` fixture uses a file lock and a shared cache directory specifically for xdist-safe downloads.

> **Tip:** If you enable parallelism with `-n`, keep the default `--dist=loadscope`. It matches the suite’s class-based design much better than fine-grained distribution.

## Suite-specific pytest options

Beyond standard pytest options, `conftest.py` adds a small set of suite-specific flags:

```python
analyze_with_ai_group.addoption("--analyze-with-ai", action="store_true", help="Analyze test failures using AI")

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

Here is what those flags actually do:

| Option | Behavior |
| --- | --- |
| `--skip-teardown` | Preserves resources after the run instead of deleting them |
| `--skip-data-collector` | Disables failure data collection and must-gather capture |
| `--data-collector-path` | Changes where collector output is written; default is `.data-collector` |
| `--analyze-with-ai` | Enriches failure reporting through the JUnit XML path after the run |
| `--openshift-python-wrapper-log-debug` | Sets `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG` during session startup |

The real-run guard for required config lives here too:

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

And teardown/data collection behavior is handled here:

```python
if not session.config.getoption("skip_data_collector"):
    collect_created_resources(session_store=_session_store, data_collector_path=_data_collector_path)

if session.config.getoption("skip_teardown"):
    LOGGER.warning("User requested to skip teardown of resources")
else:
    session_teardown(session_store=_session_store)
```

> **Warning:** `--skip-teardown` is a debugging tool, not a normal operating mode. If you use it, expect to clean up VMs, Plans, Providers, namespaces, and any source-side cloned resources yourself.

> **Note:** The current help text for `--skip-data-collector` is misleading. The implementation uses it as a true skip flag: when it is set, the suite does not collect resource metadata or run must-gather on failures.

> **Tip:** If you are building a complex selection expression, use `--collect-only` first. Once the collected set looks right, rerun the same command without dry-run mode.
