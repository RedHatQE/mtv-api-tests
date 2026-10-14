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
| `source_provider` | Yes (or a pool, see below) | Selects the source provider entry from `.providers.json` |
| `source_provider_type` | Pool alternative | Identifies the candidate pool for latency-based selection (required for pool mode) |
| `source_provider_version` | Optional | Narrows the pool to an exact version; omitted, every version of the type competes |
| `source_provider_feature` | Optional | Feature gate for the pool (`copyoffload`): copy-offload-capable providers are excluded without it and included with it |
| `storage_class` | Yes | Sets the target OpenShift storage class for migration |
| `cluster_host` | Optional | Cluster API URL. Falls back to the `CLUSTER_HOST` environment variable |
| `cluster_username` | Optional | Cluster user. Falls back to the `CLUSTER_USERNAME` environment variable |
| `cluster_password` | Optional | Cluster password. Falls back to the `CLUSTER_PASSWORD` environment variable |

If either required key is missing, the session aborts with `pytest.exit(...)` and return code `1` before any fixture runs.

Cluster credentials resolve from the environment, in this order: the `--tc` value first, then the environment variable. `CLUSTER_VERIFY_SSL` takes
precedence over `insecure_verify_skip` for OpenShift API SSL verification, and its semantics are inverted: `CLUSTER_VERIFY_SSL=true` means `insecure_verify_skip=False`.

> **Note:** `cluster_host`, `cluster_username` and `cluster_password` have **no built-in default**. All three resolved values must be nonblank unless all three keys are absent
> from both pytest config and the environment. When all three are absent, `get_cluster_client()` authenticates from the ambient kubeconfig — the path a direct `pytest` run
> takes when a kubeconfig is available to the test process. `mtv-api-tests run` takes that path only when no OpenShift provider is selected from `.providers.json`: the CLI
> checks for an existing `oc` token (`oc whoami -t`) only then, and with an OpenShift provider entry present — as in the example configuration — it instead uses that provider's
> `host`, `username`, and `password`. When any of the three keys is provided but resolves to an absent, empty, or whitespace-only value, the run fails fast with a `ValueError`
> naming each such value, for example
> `ValueError("Missing cluster credentials: set 'cluster_username' in pytest config or CLUSTER_USERNAME")`. That happens as the `ocp_admin_client` fixture requests the
> client, before any migration work, and the message says which variable to set.

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

## Latency-Based Provider Selection

`source_provider` can be resolved by latency instead of being pinned explicitly. Pass a pool instead of a key:

```bash
--tc=source_provider_type:vsphere --tc=source_provider_version:8.0.1   # exact version
--tc=source_provider_type:vsphere                                     # every version of the type
```

A feature key (`--tc=source_provider_feature:copyoffload`) flips pool membership: copy-offload-capable providers —
entries with a `copyoffload` section — are excluded from the pool by default and included only when the key is
passed. The session-scoped `selected_source_provider` fixture — requested first by the
autouse fixture, before anything consumes the provider — derives the candidate pool from `.providers.json`
(entries matching the type, and the version when passed, gated by the feature key — `copyoffload` providers only
with the key, excluded without it). **OpenShift pools are never probed**: `create_source_provider` rewrites
OpenShift sources to the local cluster host, so their configured endpoints are unused — the lowest provider key
is selected deterministically. A pool with a single member — or whose members all share one probe endpoint,
e.g. two OVA entries on the same NFS server — is selected instantly without probing: no probe pod is created and
the lowest provider key wins. Otherwise the fixture launches one probe pod on the cluster and measures TCP-connect
RTT to every **unique** candidate endpoint (providers sharing a `host:port` are measured once and share the
measurement). Each endpoint's host comes from `fqdn` when set, else from `api_url` (a URL authority or an NFS
export path — OVA entries carry an empty `fqdn` and an NFS `api_url`), else from the OpenShift `host` field; the
port comes from an explicit port in `api_url` when it carries one (e.g. OpenStack 13000), otherwise the type's API
port — OVA NFS 2049, Hyper-V 5986, everything else 443. OVA endpoints are probed on the NFS port (2049) because
that is the data path Forklift's populator mounts. Selection is the lowest RTT with the lowest provider key as the
deterministic tiebreak — median of three connects.
In a single-process run the pool is probed inline; under xdist the first worker to hold a run-scoped file lock
(`PYTEST_XDIST_TESTRUNUID`-keyed, so parallel runs never share it) probes and writes the selection while every other
worker reads the same file. The chosen provider and its RTT are logged.

Precedence and failure modes:

- Explicit `--tc=source_provider:<key>` always wins: nothing is probed and nothing changes.
- The pool form requires the type key; version is optional and `source_provider_version` without
  `source_provider_type` aborts the session with `pytest.exit(...)`.
- Neither an explicit provider nor a pool aborts with the missing-config error, as before.
- An empty pool (no type match, or no provider passes the feature gate) aborts the session — never a skip.
- An entry with no resolvable endpoint host (empty `fqdn`, `api_url`, and `host`) raises a `ValueError` naming the
type — it is never probed against an empty host (OpenShift entries are exempt: they are never probed at all).
- Unreachable candidates are excluded from selection; a fully unreachable pool aborts the session.
- In pool mode the provider is unknown at collection time, so collection-time provider gating uses the pool's
  `source_provider_type` and item names carry the type instead of the provider key.

For copy-offload (XCOPY) plans the bulk disk copy runs array-side (storage array / ESXi hosts, including
`dedicated_migration_hosts`) and does not traverse the cluster-to-provider path: the probe measures the vCenter API
path only, so selection stays valid for copy-offload pools but the runtime win applies to cold/warm (NBD)
migrations. Pool members are expected to be true clones of each other (same VM inventory, same capabilities); the
feature key enforces copy-offload capability, VM inventory equivalence is the operator's responsibility.

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
- Class setup/call failures queue `oc adm must-gather` until the pre-finalizer class-plan boundary, attempting at most one successful gather per class-plan instance per worker. Operational class-plan failures remain pending for session-finish retry; artifacts are not guaranteed if retry fails. Standalone tests attempt collection per failing phase until one succeeds, but failed attempts are not queued for session-finish retry. Subdirectories use percent-encoded node identities, class parameter indices where applicable, and worker IDs.
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
