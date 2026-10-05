# Runtime Configuration

`mtv-api-tests` uses `pytest-testconfig` for suite settings and plain pytest flags for per-run behavior. In practice, you keep shared defaults in `tests/tests_config/config.py`,
keep provider definitions in `.providers.json`, and pass environment-specific values such as `source_provider` and `storage_class` with `--tc=key:value`.

Most users only need to remember three things:

1. `pytest.ini` already loads the default runtime config file for you.
2. A normal test run requires `source_provider` and `storage_class`.
3. The repo adds custom pytest flags for cleanup, artifact collection, logging, provider-file selection, and AI analysis.

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
  --show-progress
  --strict-markers
  --dist=loadscope
```

What this means in day-to-day use:

- Tests are collected from `tests/`.
- `pytest-testconfig` automatically loads `tests/tests_config/config.py`.
- JUnit XML is written by default to `junit-report.xml`.
- The suite enables strict marker validation and xdist `loadscope` distribution.
- Pytest's built-in logging plugin is disabled, and the suite configures its own logger.

> **Note:** JIRA integration is installed but opt-in. `pytest-jira` is a project dependency, and it registers `--jira` with a default of `False`. `--jira` is **not** part of
> `addopts`. Add it yourself when you want JIRA-linked markers to drive test behavior; `jira.cfg.example` shows the expected `jira.cfg` format.

## Required Runtime Overrides

For a normal run, the suite expects two runtime values even though they are not defined in the default config file:

| Key | Required | Purpose |
| --- | --- | --- |
| `source_provider` | Yes | Selects the source provider entry from `.providers.json` |
| `storage_class` | Yes | Sets the target OpenShift storage class for migration |
| `cluster_host` | Optional | Cluster API URL. Falls back to the `CLUSTER_HOST` environment variable |
| `cluster_username` | Optional | Cluster user. Falls back to the `CLUSTER_USERNAME` environment variable |
| `cluster_password` | Optional | Cluster password. Falls back to the `CLUSTER_PASSWORD` environment variable |
| `target_ocp_version` | Optional | Used only for the generated VM name suffix, where dots are replaced with dashes |

If either required key is missing, the session aborts with `pytest.exit(...)` and return code `1` before any fixture runs.

Cluster credentials resolve from the environment, in this order: the `--tc` value first, then the environment variable. `CLUSTER_VERIFY_SSL` takes
precedence over `insecure_verify_skip` for OpenShift API SSL verification, and its semantics are inverted: `CLUSTER_VERIFY_SSL=true` means `insecure_verify_skip=False`.

> **Note:** `cluster_host`, `cluster_username` and `cluster_password` have **no built-in default**. When neither `--tc` nor the environment supplies a value,
> `get_cluster_client()` passes `None` through to `get_client()` and the failure surfaces from the OpenShift client library as a connection error. Supply all three
> explicitly or the run fails before any fixture executes.

The repository's OpenShift Job template in `README.md` uses `--tc=` overrides like this:

```bash
uv run pytest -m "[TEST_MARKERS]" \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:[SOURCE_PROVIDER] \
  --tc=storage_class:[STORAGE_CLASS]
```

> **Warning:** `source_provider` is not the provider type. It must match a top-level key in `.providers.json` exactly.

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
| --- | --- | --- |
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
| --- | --- |
| `number_of_vms` | `1` |
| `check_vms_signals` | `True` |
| `vm_name_search_pattern` | `""` |

> **Warning:** For boolean-style CLI overrides such as `insecure_verify_skip` and `source_provider_insecure_skip_verify`, use lowercase string values like `true` and `false`.
> The default config file stores them as strings, and some code paths handle them that way.

## Named Test Plans In `tests_params`

The same config file also contains `tests_params`, which is the catalog of named migration scenarios used by the test classes. These are not suite-wide defaults; they are
per-scenario plan definitions.

A minimal real entry from `tests/tests_config/config.py`:

```python
"test_sanity_cold_mtv_migration": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8", "guest_agent": True, "add_nic": True, "add_nic_start_connected": False},
    ],
    "warm_migration": False,
    "per_nic_network_map": True,
},
```

Those named plans are referenced directly by the tests through `class_plan_config` with `indirect=True`:

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

[Test Plan Configuration](test-plan-configuration.html) documents every per-VM key and plan-level flag the code supports, with the real names.

## Custom Pytest Options

The repository adds six user-facing pytest options on top of the standard pytest set:

| Option | Group |
| --- | --- |
| `--providers-json <path>` | Providers |
| `--skip-data-collector` | DataCollector |
| `--data-collector-path <path>` | DataCollector |
| `--skip-teardown` | Teardown |
| `--openshift-python-wrapper-log-debug` | Openshift Python Wrapper |
| `--analyze-with-ai` | Analyze with AI |

### Provider file selection

Resolution order when the suite loads provider definitions:

1. `--providers-json` CLI value.
2. `PROVIDERS_JSON_PATH` environment variable.
3. `.providers.json` in the current working directory.

A missing path raises `FileNotFoundError`, and an empty or non-mapping file fails fast. See [Provider Config File](provider-config-file.html).

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

- Disables itself in dry-run mode (`--collect-only`, `--setup-plan`).
- Calls `load_dotenv()`, so a local `.env` file can supply the settings.
- Requires `ROOTCOZ_SERVER_URL`; if unset, it logs a warning and turns the feature off.
- After a failed run, reads the JUnit XML file and posts the raw XML to `${ROOTCOZ_SERVER_URL}/analyze-failures`.
- If enrichment succeeds, writes the enriched XML back to the same file.

Environment variables used by this feature:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ROOTCOZ_SERVER_URL` | Yes | None | Base URL of the analysis service |
| `ROOTCOZ_TIMEOUT` | No | `600` | Request timeout in seconds. Invalid values fall back to `600` |
| `ROOTCOZ_AI_PROVIDER` | No | None | AI provider name sent to the service. When unset, the service applies its own default |
| `ROOTCOZ_AI_MODEL` | No | None | AI model name sent to the service. When unset, the service applies its own default |

> **Note:** pytest does not hard-code a provider or model default. Those defaults live in `.rootcoz/settings.json`, which is owned by the rootcoz service.

Request payload:

```python
payload: dict[str, str] = {"raw_xml": raw_xml}
if ai_provider := os.environ.get("ROOTCOZ_AI_PROVIDER"):
    payload["ai_provider"] = ai_provider
if ai_model := os.environ.get("ROOTCOZ_AI_MODEL"):
    payload["ai_model"] = ai_model
```

Important behavior to know:

- Successful runs skip AI enrichment.
- `--collect-only` and `--setup-plan` disable AI analysis automatically.
- If the JUnit XML path is unset or the file is missing, enrichment is skipped.
- If enrichment fails, the original JUnit XML is preserved.
- The default JUnit XML path is already configured as `junit-report.xml` in `pytest.ini`.

> **Warning:** `--analyze-with-ai` sends the raw JUnit XML content to an external HTTP service. Review what your report contains before enabling this in shared or external
> environments.

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

> **Tip:** If you start the published container image without overriding its command, it only performs test collection. To run real tests, supply your own `uv run pytest ...`
> command.
