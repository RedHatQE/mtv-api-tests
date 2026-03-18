# Quickstart First Run

`mtv-api-tests` uses `pytest` with `pytest-testconfig`. The shared test catalog is already wired into the repo, so a first run is mostly about three things:

1. Create `.providers.json` in the repository root.
2. Pass the environment-specific runtime values with `--tc=...`.
3. Start with `--collect-only` or `--setup-plan`, then run one small sanity class.

This page assumes you are running from the repository root with project dependencies already available.

## How Configuration Works

`pytest.ini` already tells pytest to load `tests/tests_config/config.py` through `pytest-testconfig`:

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
```

For real test execution, the suite requires two runtime keys:

```python
def pytest_sessionstart(session):
    required_config = ("storage_class", "source_provider")
```

> **Note:** You normally do not need to pass `--tc-file` yourself. The repo already points pytest at `tests/tests_config/config.py`, so the usual workflow is to add runtime overrides with `--tc=key:value`.

## Create `.providers.json`

The provider loader looks for `.providers.json` in the current working directory and parses it as JSON:

```python
def load_source_providers() -> dict[str, dict[str, Any]]:
    providers_file = Path(".providers.json")
    if not providers_file.exists():
        return {}

    with open(providers_file) as fd:
        content = fd.read()
        if not content.strip():
            return {}
        return json.loads(content)
```

Create `.providers.json` in the repository root. Use `.providers.json.example` as the starting template. This is the vSphere block from that file:

```jsonc
"vsphere": {
  "type": "vsphere",
  "version": "<SERVER VERSION>",
  "fqdn": "SERVER FQDN/IP",
  "api_url": "<SERVER FQDN/IP>/sdk",
  "username": "USERNAME",
  "password": "PASSWORD",  # pragma: allowlist secret
  "guest_vm_linux_user": "LINUX VMS USERNAME",
  "guest_vm_linux_password": "LINUX VMS PASSWORD",  # pragma: allowlist secret
  "guest_vm_win_user": "WINDOWS VMS USERNAME",
  "guest_vm_win_password": "WINDOWS VMS PASSWORD",  # pragma: allowlist secret
  "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
}
```

The same example file also includes templates for `ovirt`, `openstack`, `openshift`, and `ova`.

> **Warning:** `.providers.json.example` is not valid JSON as-is. It contains comments and placeholder values. Your real `.providers.json` must be valid JSON, because the loader uses `json.loads(...)`.

> **Note:** The top-level provider key is what you pass later as `--tc=source_provider:<key>`. If your file uses `"vsphere"`, then your runtime flag must use `--tc=source_provider:vsphere`.

> **Warning:** `.providers.json` contains credentials. Keep it local and treat it as sensitive.

## Know What the Built-In Plans Expect

The shared defaults and named test plans live in `tests/tests_config/config.py`. These are the defaults most relevant to a first run:

```python
insecure_verify_skip: str = "true"
source_provider_insecure_skip_verify: str = "false"
target_namespace_prefix: str = "auto"
mtv_namespace: str = "openshift-mtv"
remote_ocp_cluster: str = ""
plan_wait_timeout: int = 3600
```

For a first targeted migration, the smallest built-in cold plan is:

```python
"test_sanity_cold_mtv_migration": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8", "guest_agent": True},
    ],
    "warm_migration": False,
},
```

The warm equivalent is:

```python
"test_sanity_warm_mtv_migration": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel8",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
},
```

> **Tip:** If you want the smoothest first run, prepare a source VM named `mtv-tests-rhel8` and start with the cold sanity plan. If your lab uses different VM names, update the matching entry in `tests/tests_config/config.py` before your first real run.

## Pass Runtime Settings With `pytest-testconfig`

For a first run, these are the settings you will usually care about:

- `source_provider`: required for real execution; must match a top-level key in `.providers.json`
- `storage_class`: required for real execution; destination OpenShift storage class
- `cluster_host`, `cluster_username`, `cluster_password`: useful when you want to pass OpenShift access explicitly on the command line
- `mtv_namespace`: optional; defaults to `openshift-mtv`
- `target_namespace_prefix`: optional; defaults to `auto`
- `insecure_verify_skip`: optional; controls OpenShift API TLS verification
- `source_provider_insecure_skip_verify`: optional; controls source-provider TLS verification

A typical first-run command shape is:

```bash
uv run pytest -v tests/test_mtv_cold_migration.py::TestSanityColdMtvMigration \
  --tc=source_provider:vsphere \
  --tc=storage_class:<storage-class> \
  --tc=cluster_host:https://api.<cluster>:6443 \
  --tc=cluster_username:<username> \
  --tc=cluster_password:${CLUSTER_PASSWORD}
```

If your provider key is not `vsphere`, replace it with the top-level key you actually used in `.providers.json`.

If you need to relax source-provider TLS verification in a lab environment, add:

```bash
--tc=source_provider_insecure_skip_verify:true
```

If your MTV operator is not installed in `openshift-mtv`, add:

```bash
--tc=mtv_namespace:<your-mtv-namespace>
```

## Start With `--collect-only` Or `--setup-plan`

This repo treats both `--collect-only` and `--setup-plan` as normal dry-run entry points. In fact, `tox.toml` uses both as a basic pytest check, and the container image in `Dockerfile` defaults to `uv run pytest --collect-only`.

Use `--collect-only` when you want to confirm what pytest will select:

```bash
uv run pytest --collect-only -q tests/test_mtv_cold_migration.py::TestSanityColdMtvMigration
```

Use `--setup-plan` when you want to see fixture/setup planning for the same target before a real run:

```bash
uv run pytest --setup-plan tests/test_mtv_cold_migration.py::TestSanityColdMtvMigration \
  --tc=source_provider:vsphere \
  --tc=storage_class:<storage-class>
```

If you want to browse by marker first, the built-in markers are:

- `tier0`
- `warm`
- `remote`
- `copyoffload`

For example, to list the smoke-suite tests:

```bash
uv run pytest --collect-only -q -m tier0
```

> **Tip:** `--collect-only` is the safest place to start when you want to confirm test names, markers, and node IDs before wiring in all runtime values.

## Run The First Targeted Test Class

The cold sanity test is defined in `tests/test_mtv_cold_migration.py` like this:

```python
@pytest.mark.tier0
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_sanity_cold_mtv_migration"],
        )
    ],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSanityColdMtvMigration:
    """Cold migration test - sanity check."""
```

That class runs the migration as five dependent steps:

- `test_create_storagemap`
- `test_create_networkmap`
- `test_create_plan`
- `test_migrate_vms`
- `test_check_vms`

Run the whole class, not a single method:

```bash
uv run pytest -v tests/test_mtv_cold_migration.py::TestSanityColdMtvMigration \
  --tc=source_provider:vsphere \
  --tc=storage_class:<storage-class> \
  --tc=cluster_host:https://api.<cluster>:6443 \
  --tc=cluster_username:<username> \
  --tc=cluster_password:${CLUSTER_PASSWORD}
```

If you specifically want to try the warm sanity path afterward, use:

```bash
uv run pytest -v tests/test_mtv_warm_migration.py::TestSanityWarmMtvMigration \
  --tc=source_provider:vsphere \
  --tc=storage_class:<storage-class> \
  --tc=cluster_host:https://api.<cluster>:6443 \
  --tc=cluster_username:<username> \
  --tc=cluster_password:${CLUSTER_PASSWORD}
```

> **Warning:** Warm tests are explicitly skipped in this repo for `openstack`, `openshift`, and `ova` providers. For a first run, the cold sanity class is the safer starting point.

## Common First-Run Problems

If the run fails early, check these first:

- `.providers.json` is missing or empty.
- The value passed in `--tc=source_provider:...` does not exactly match a top-level key in `.providers.json`.
- `.providers.json` was copied from `.providers.json.example` without removing comments.
- The built-in sanity plan expects a source VM named `mtv-tests-rhel8`, but that VM does not exist in your provider.
- You targeted a single method such as `::test_create_plan` instead of the full class.
- You chose a warm test on a provider type that the repo skips for warm migration.

> **Tip:** By default, the suite writes `junit-report.xml` and tears down created resources after the run. If you need to inspect what was created after a failure, rerun with `--skip-teardown`.
