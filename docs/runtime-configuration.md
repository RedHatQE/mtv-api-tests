# Runtime Configuration

`mtv-api-tests` uses `pytest-testconfig` for suite settings and plain pytest flags for per-run behavior. In practice, you keep shared defaults in `tests/tests_config/config.py`, keep provider definitions in `.providers.json`, and pass environment-specific values such as `source_provider` and `storage_class` with `--tc=key:value`.

Most users only need to remember three things:

1. `pytest.ini` already loads the default runtime config file for you.
2. A normal test run requires `source_provider` and `storage_class`.
3. The repo adds several custom pytest flags for cleanup, artifact collection, logging, and AI analysis.

## How Configuration Is Loaded

The baseline pytest behavior is defined in `pytest.ini`:

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

What this means in day-to-day use:

- Tests are collected from `tests/`.
- `pytest-testconfig` automatically loads `tests/tests_config/config.py`.
- JUnit XML is written by default to `junit-report.xml`.
- The suite enables strict marker validation and xdist `loadscope` distribution.
- Pytest's built-in logging plugin is disabled, and the suite configures its own logger.

> **Note:** The default `addopts` also enable `--jira`. If you use JIRA-linked tests, `jira.cfg.example` shows the expected file format.

## Required Runtime Overrides

For a normal run, the suite expects two runtime values even though they are not defined in the default config file:

| Key | Required | Purpose |
|---|---|---|
| `source_provider` | Yes | Selects the source provider entry from `.providers.json` |
| `storage_class` | Yes | Sets the target OpenShift storage class for migration |
| `cluster_host` | Optional | Passed into cluster client creation when supplied |
| `cluster_username` | Optional | Passed into cluster client creation when supplied |
| `cluster_password` | Optional | Passed into cluster client creation when supplied |
| `target_ocp_version` | Optional | Used only for generated VM suffix naming |

The repository's own copy-offload job example uses `--tc=` overrides like this:

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

> **Warning:** `source_provider` is not the provider type. It must match a key in `.providers.json` exactly.

The provider definitions themselves are loaded from `.providers.json` in the repository root. If that file is missing, the suite cannot resolve the requested provider.

## Global `pytest-testconfig` Values

The default shared values live at the top of `tests/tests_config/config.py`:

```python
global config

insecure_verify_skip: str = "true"  # SSL verification for OCP API connections
source_provider_insecure_skip_verify: str = "false"  # SSL verification for source provider (VMware, RHV, etc.)
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace_prefix: str = "auto"
mtv_namespace: str = "openshift-mtv"
vm_name_search_pattern: str = ""
remote_ocp_cluster: str = ""
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600
```

### Actively used global values

| Key | Default | What it controls |
|---|---|---|
| `insecure_verify_skip` | `"true"` | OpenShift API SSL verification. The cluster client is created with `verify_ssl=not insecure_verify_skip`. |
| `source_provider_insecure_skip_verify` | `"false"` | Source-provider SSL verification and the Provider secret's `insecureSkipVerify` value. |
| `target_namespace_prefix` | `"auto"` | Base text used when generating the target namespace name for migrated resources. |
| `mtv_namespace` | `"openshift-mtv"` | Namespace used for MTV resources, pod health checks, and must-gather collection. |
| `remote_ocp_cluster` | `""` | Enables tests marked `remote` when set and validates the current cluster host against that value. |
| `snapshots_interval` | `2` | Updates the `forklift-controller` precopy interval for warm migration tests. |
| `mins_before_cutover` | `5` | Number of minutes added when calculating warm-migration cutover time. |
| `plan_wait_timeout` | `3600` | Timeout used while waiting for migration plans to complete. |

### Defined in the default file but not currently used elsewhere

A repository-wide search shows these keys are defined in `tests/tests_config/config.py` but are not referenced by the rest of the codebase today:

| Key | Default |
|---|---|
| `number_of_vms` | `1` |
| `check_vms_signals` | `True` |
| `vm_name_search_pattern` | `""` |

> **Warning:** For boolean-style CLI overrides such as `insecure_verify_skip` and `source_provider_insecure_skip_verify`, use lowercase string values like `true` and `false`. The default config file stores them as strings, and some code paths handle them that way.

## Named Test Plans In `tests_params`

The same config file also contains `tests_params`, which is the catalog of named migration scenarios used by the test classes. These are not suite-wide defaults; they are per-scenario plan definitions.

A real example from `tests/tests_config/config.py`:

```python
"test_warm_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2022-ip-3disks",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "vm_target_namespace": "custom-vm-namespace",
    "multus_namespace": "default",  # Cross-namespace NAD access
    "pvc_name_template": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
    "pvc_name_template_use_generate_name": True,
    "target_labels": {
        "mtv-comprehensive-test": None,  # None = auto-generate with session_uuid
        "static-label": "static-value",
    },
    "target_affinity": {
        "podAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": {"app": "comprehensive-test"}},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                    "weight": 75,
                }
            ]
        }
    },
},
```

Those named plans are referenced directly by the tests. For example:

```python
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_migration_comprehensive"],
        )
    ],
    indirect=True,
    ids=["comprehensive-warm"],
)
```

Common `tests_params` keys you will see in this repository include:

- `virtual_machines` for the VM list and per-VM options such as `name`, `source_vm_power`, `guest_agent`, `clone`, and `disk_type`.
- `warm_migration` and `copyoffload` for the main migration mode.
- `target_power_state`, `preserve_static_ips`, `vm_target_namespace`, `multus_namespace`, `pvc_name_template`, `target_labels`, and `target_affinity` for plan behavior.
- `pre_hook`, `post_hook`, and `expected_migration_result` for hook-driven scenarios.
- `guest_agent_timeout` for scenarios that need a longer wait after migration.

## Custom Pytest Options

The repository adds four user-facing runtime features on top of standard pytest options: artifact collection, teardown control, extra logging, and AI analysis.

### Data collection

By default, the suite collects runtime artifacts for failed runs.

- The base directory defaults to `.data-collector`.
- On test failure, the suite attempts to run `oc adm must-gather` into a per-test subdirectory under that path.
- At session finish, it writes a `resources.json` file with tracked resources.
- If session teardown fails, it attempts an additional must-gather into the base collector directory.

Use these flags to control that behavior:

- `--skip-data-collector` disables artifact collection.
- `--data-collector-path <path>` changes the output directory.

> **Warning:** The suite deletes and recreates the base data-collector directory at session start. Use a dedicated path if you want to preserve older artifacts.

> **Note:** The current help text for `--skip-data-collector` is misleading. In actual runtime behavior, the flag disables data collection.

> **Tip:** When `resources.json` is available, the repository includes `tools/clean_cluster.py` to clean resources from that file.

### Teardown control

By default, the suite cleans up the resources it created.

That includes:

- Session-level tracked resources such as plans, providers, namespaces, and migrated resources.
- Class-level cleanup of migrated VMs through the `cleanup_migrated_vms` fixture.

Use `--skip-teardown` when you want to keep resources around for debugging.

The repository's own documentation shows it this way:

```bash
uv run pytest -m copyoffload --skip-teardown \
  -v \
  ...
```

> **Warning:** `--skip-teardown` is for investigation and debugging. If you use it, you are responsible for cleaning up leftover resources afterward.

### Debug logging

Logging is already enabled by default through `pytest.ini`:

- `-s` keeps stdout/stderr visible.
- `-o log_cli=true` enables live console logging.
- The suite writes logs to `pytest-tests.log` unless you set a different `--log-file`.
- The log level comes from pytest's `log_cli_level` option and falls back to `INFO`.

There is also one custom debug flag:

- `--openshift-python-wrapper-log-debug` sets `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG`.

In other words, the main knobs for troubleshooting are:

- Standard pytest logging options such as `log_cli_level` and `log_file`.
- The custom `--openshift-python-wrapper-log-debug` flag for wrapper internals.

> **Tip:** For deeper troubleshooting, raise `log_cli_level`, write to a dedicated `--log-file`, and add `--openshift-python-wrapper-log-debug`.

### AI failure analysis

The suite can enrich failed JUnit XML reports with AI-generated analysis.

Enable it with:

- `--analyze-with-ai`

When enabled, the code does the following:

- Calls `load_dotenv()`, so a local `.env` file can supply the settings.
- Checks `JJI_SERVER_URL`.
- Uses default values for provider and model if you did not set them.
- After a failed run, reads the JUnit XML file and posts the raw XML to `${JJI_SERVER_URL}/analyze-failures`.
- If enrichment succeeds, writes the enriched XML back to the same file.

Environment variables used by this feature:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JJI_SERVER_URL` | Yes | None | URL of the analysis service |
| `JJI_AI_PROVIDER` | No | `claude` | AI provider name sent to the service |
| `JJI_AI_MODEL` | No | `claude-opus-4-6[1m]` | AI model name sent to the service |
| `JJI_TIMEOUT` | No | `600` | Request timeout in seconds |

Important behavior to know:

- Successful runs skip AI enrichment.
- `--collect-only` and `--setup-plan` disable AI analysis automatically.
- If the JUnit XML file is missing, enrichment is skipped.
- The default JUnit XML path is already configured as `junit-report.xml` in `pytest.ini`.

> **Warning:** `--analyze-with-ai` sends the raw JUnit XML content to an external HTTP service. Review what your report contains before enabling this in shared or external environments.

> **Note:** If enrichment succeeds, the original JUnit XML file is overwritten in place with the enriched version.

## Dry-Run Modes

This repository treats `--collect-only` and `--setup-plan` as dry-run modes.

That behavior is visible in `tox.toml`:

```toml
[env.pytest-check]
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

And the container image defaults to collection-only mode:

```dockerfile
CMD ["uv", "run", "pytest", "--collect-only"]
```

In dry-run mode:

- Required runtime checks for `source_provider` and `storage_class` are skipped.
- AI analysis is disabled.
- Session-finish teardown and JUnit enrichment do not run.

> **Tip:** If you start the published container image without overriding its command, it only performs test collection. To run real tests, supply your own `uv run pytest ...` command.
