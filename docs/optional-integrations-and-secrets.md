# Optional Integrations And Secrets

`mtv-api-tests` keeps its optional integrations outside the main test logic. In practice, that means you only add extra local files or environment variables when you want one of these features:

- JIRA-aware test behavior through `pytest-jira`
- AI-powered enrichment of failed JUnit reports
- Copy-offload credential overrides for storage and ESXi access

> **Note:** The repository already ignores the most important local-secret files: `.providers.json`, `jira.cfg`, `.env`, and `junit-report.xml`.

## JIRA Integration

JIRA support is already wired into the test runner. The project depends on `pytest-jira`, and `pytest.ini` enables the plugin for normal test runs.

Relevant excerpt from `pytest.ini`:

```ini
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

To connect that integration to your JIRA instance, use the shipped template in `jira.cfg.example` and create a local `jira.cfg` with the same shape:

```ini
[DEFAULT]
url = <Jira URL>
token = <User Token>
```

The current codebase uses JIRA markers to gate specific tests around known issues. In both `tests/test_mtv_warm_migration.py` and `tests/test_warm_migration_comprehensive.py`, the RHV warm-migration path is annotated like this:

```python
# Only apply Jira marker for RHV - skip if issue unresolved, run normally if resolved
if _SOURCE_PROVIDER_TYPE == Provider.ProviderType.RHV:
    pytestmark.append(pytest.mark.jira("MTV-2846", run=False))
```

That is the important user-facing behavior: JIRA is not just decorative metadata here. It is used to decide whether some tests should run.

> **Tip:** Keep `jira.cfg` local, or generate it at job runtime from your CI secret store. The repo already ignores `jira.cfg`, so there is no reason to commit a token.

## AI Failure Analysis

AI failure analysis is opt-in. It only activates when you pass `--analyze-with-ai`.

From `conftest.py`:

```python
analyze_with_ai_group = parser.getgroup(name="Analyze with AI")
analyze_with_ai_group.addoption("--analyze-with-ai", action="store_true", help="Analyze test failures using AI")
```

When that flag is present, the suite loads `.env`, checks for a JJI server URL, and fills in defaults for the provider and model if you did not set them yourself.

From `utilities/pytest_utils.py`:

```python
load_dotenv()

LOGGER.info("Setting up AI-powered test failure analysis")

if not os.environ.get("JJI_SERVER_URL"):
    LOGGER.warning("JJI_SERVER_URL is not set. Analyze with AI features will be disabled.")
    session.config.option.analyze_with_ai = False

else:
    if not os.environ.get("JJI_AI_PROVIDER"):
        os.environ["JJI_AI_PROVIDER"] = "claude"

    if not os.environ.get("JJI_AI_MODEL"):
        os.environ["JJI_AI_MODEL"] = "claude-opus-4-6[1m]"
```

The current environment variables are:

| Variable | Required | Default in code | What it controls |
| --- | --- | --- | --- |
| `JJI_SERVER_URL` | Yes | none | Base URL of the Jenkins Job Insight service |
| `JJI_AI_PROVIDER` | No | `claude` | Provider name sent to JJI |
| `JJI_AI_MODEL` | No | `claude-opus-4-6[1m]` | Model name sent to JJI |
| `JJI_TIMEOUT` | No | `600` | HTTP timeout in seconds for the analysis request |

> **Note:** `claude-opus-4-6[1m]` above is the exact current default string in the code.

The suite already writes a JUnit report by default because `pytest.ini` sets `--junit-xml=junit-report.xml`. When there are failures, the AI integration reads that XML, posts it to the JJI service, and writes the enriched XML back to the same file.

From `utilities/pytest_utils.py`:

```python
response = requests.post(
    f"{server_url.rstrip('/')}/analyze-failures",
    json={
        "raw_xml": raw_xml,
        "ai_provider": ai_provider,
        "ai_model": ai_model,
    },
    timeout=timeout_value,
)
```

A few practical details matter here:

- If `JJI_SERVER_URL` is missing, the feature disables itself with a warning.
- If `JJI_TIMEOUT` is invalid, the code falls back to `600`.
- Dry-run modes such as `--collectonly` and `--setupplan` disable the feature.
- Successful runs skip enrichment because there are no failures to analyze.
- If enrichment fails, the original JUnit XML is preserved.

> **Warning:** The AI path sends the raw JUnit XML to `JJI_SERVER_URL/analyze-failures`. That report can contain test names, failure messages, resource names, and any other details included in the report. Only enable this against a service you trust.

> **Tip:** Because `.env` is gitignored and only loaded when `--analyze-with-ai` is enabled, it is a good place for local `JJI_*` settings.

## Copy-Offload Credential Overrides

Copy-offload is the most secret-heavy optional path in the repository. It is also the strictest one: the fixtures fail early if required copy-offload configuration is missing.

The base configuration lives under the source provider entry in `.providers.json`, which the suite loads from the repository root with `Path(".providers.json")` and `json.loads(...)`.

A relevant excerpt from `.providers.json.example` shows the expected shape:

```jsonc
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
  # This is the vSphere datastore ID (e.g., "datastore-12345") where VMs reside
  # Get via vSphere: Datacenter → Storage → Datastore → Summary → More Objects ID
  "datastore_id": "datastore-12345",

  # Optional: Secondary datastore for multi-datastore copy-offload tests
  # Only needed when testing VMs with disks spanning multiple datastores
  # When specified, tests can validate copy-offload with disks on different datastores
  "secondary_datastore_id": "datastore-67890",

  # Optional: Non-XCOPY datastore for mixed datastore tests
  # This should be a datastore that does NOT support XCOPY/VAAI primitives
  # Used for testing VMs with disks on both XCOPY and non-XCOPY datastores
  "non_xcopy_datastore_id": "datastore-99999",

  "default_vm_name": "rhel9-template",
  "storage_hostname": "storage.example.com",
  "storage_username": "admin",
  "storage_password": "your-password-here",  # pragma: allowlist secret
```

And for SSH-based cloning, the same example file includes:

```jsonc
# ESXi SSH configuration (optional, for SSH-based cloning):
# Can be overridden via environment variables: COPYOFFLOAD_ESXI_HOST, COPYOFFLOAD_ESXI_USER, COPYOFFLOAD_ESXI_PASSWORD
"esxi_clone_method": "ssh",  # "vib" (default) or "ssh"
"esxi_host": "your-esxi-host.example.com",  # required for ssh method
"esxi_user": "root",  # required for ssh method
"esxi_password": "your-esxi-password",  # pragma: allowlist secret # required for ssh method
```

> **Warning:** The comments in `.providers.json.example` are not valid JSON. The real `.providers.json` file is parsed with `json.loads(...)`, so remove the `# pragma: allowlist secret` comments when creating your own file.

### How Environment Overrides Work

The override rule is simple and explicit. For the fields that support overrides, environment variables win over values from `.providers.json`.

From `utilities/copyoffload_migration.py`:

```python
env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
return os.getenv(env_var_name) or copyoffload_config.get(credential_name)
```

That helper is used for the credential-like copy-offload inputs. In the current code, the supported override names are:

| `.providers.json` key | Environment variable |
| --- | --- |
| `storage_hostname` | `COPYOFFLOAD_STORAGE_HOSTNAME` |
| `storage_username` | `COPYOFFLOAD_STORAGE_USERNAME` |
| `storage_password` | `COPYOFFLOAD_STORAGE_PASSWORD` |
| `ontap_svm` | `COPYOFFLOAD_ONTAP_SVM` |
| `vantara_storage_id` | `COPYOFFLOAD_VANTARA_STORAGE_ID` |
| `vantara_storage_port` | `COPYOFFLOAD_VANTARA_STORAGE_PORT` |
| `vantara_hostgroup_id_list` | `COPYOFFLOAD_VANTARA_HOSTGROUP_ID_LIST` |
| `pure_cluster_prefix` | `COPYOFFLOAD_PURE_CLUSTER_PREFIX` |
| `powerflex_system_id` | `COPYOFFLOAD_POWERFLEX_SYSTEM_ID` |
| `powermax_symmetrix_id` | `COPYOFFLOAD_POWERMAX_SYMMETRIX_ID` |
| `esxi_host` | `COPYOFFLOAD_ESXI_HOST` |
| `esxi_user` | `COPYOFFLOAD_ESXI_USER` |
| `esxi_password` | `COPYOFFLOAD_ESXI_PASSWORD` |

The code currently recognizes these `storage_vendor_product` values: `ontap`, `vantara`, `primera3par`, `pureFlashArray`, `powerflex`, `powermax`, `powerstore`, `infinibox`, and `flashsystem`.

Only some vendors need extra vendor-specific secret values:

- `ontap` requires `ontap_svm`
- `vantara` requires `vantara_storage_id`, `vantara_storage_port`, and `vantara_hostgroup_id_list`
- `pureFlashArray` requires `pure_cluster_prefix`
- `powerflex` requires `powerflex_system_id`
- `powermax` requires `powermax_symmetrix_id`
- `primera3par`, `powerstore`, `infinibox`, and `flashsystem` use only the base storage credentials

> **Warning:** Not every `copyoffload` key is overrideable. In the current code paths, `storage_vendor_product`, `datastore_id`, `secondary_datastore_id`, `non_xcopy_datastore_id`, `rdm_lun_uuid`, and `esxi_clone_method` are read directly from `.providers.json`, not through `COPYOFFLOAD_*` overrides.

> **Tip:** A good working pattern is to keep stable, non-secret facts in `.providers.json` and move only the sensitive pieces, such as passwords and vendor credentials, into `COPYOFFLOAD_*` environment variables.

### How Those Values Become Runtime Secrets

The copy-offload fixture converts the resolved values into a Kubernetes `Secret`. The base secret data is created like this:

```python
secret_data = {
    "STORAGE_HOSTNAME": storage_hostname,
    "STORAGE_USERNAME": storage_username,
    "STORAGE_PASSWORD": storage_password,
}
```

That secret is then referenced from the StorageMap config used by the copy-offload tests.

From `tests/test_copyoffload_migration.py`:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
    }
}
```

This is why environment overrides are useful: they let you keep the StorageMap logic unchanged while changing only the secret material injected into the run.

If you use SSH-based ESXi cloning, the vSphere provider also patches the provider setting to `esxiCloneMethod: ssh`. If you omit `esxi_clone_method` or leave it as `vib`, the code treats `vib` as the default and does not patch anything.

## Handling Sensitive Values In Practice

The safest way to work with this repository is to separate stable configuration from secrets:

- Keep stable settings in `.providers.json`: provider type, version, datastore IDs, vendor selection, VM names, and clone method.
- Keep tokens and passwords in `jira.cfg`, `.env`, or `COPYOFFLOAD_*` environment variables.
- In automation, create those files at runtime from your CI or cluster secret store instead of baking them into images or checking them into Git.
- Treat `junit-report.xml` as sensitive if it may contain failure details you would not want to share broadly, especially when AI analysis is enabled.

The existing OpenShift Job guidance in `docs/copyoffload/how-to-run-copyoffload-tests.md` already demonstrates a good secret-injection pattern for automation:

```bash
read -sp "Enter cluster password: " CLUSTER_PASSWORD && echo
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  --from-literal=cluster_host=https://api.your-cluster.com:6443 \
  --from-literal=cluster_username=kubeadmin \
  --from-literal=cluster_password="${CLUSTER_PASSWORD}" \
  -n mtv-tests
unset CLUSTER_PASSWORD
```

That pattern is preferable to hardcoding credentials in manifests or committing local config files.

There is also one explicit redaction path worth knowing about: the SSH helper masks the OpenShift token before logging the `virtctl` command.

From `utilities/ssh_utils.py`:

```python
cmd_str = " ".join(cmd)
if self.ocp_token:
    cmd_str = cmd_str.replace(self.ocp_token, "[REDACTED]")
LOGGER.info(f"Full virtctl command: {cmd_str}")
```

> **Note:** That masking is useful, but it is not a guarantee that every secret in every code path will be redacted automatically.

> **Warning:** Copy the keys from `.providers.json.example`, not the comments. The `# pragma: allowlist secret` annotations are there for repository scanning and will break a real JSON file.

> **Tip:** For day-to-day use, a practical split is:
> keep `.providers.json` for non-secret structure,
> keep `jira.cfg` and `.env` local,
> and use `COPYOFFLOAD_*` or your CI secret manager for the values you would least want to store on disk.
