# Optional Integrations And Secrets

`mtv-api-tests` keeps its optional integrations outside the main test logic. In practice, that means you only add extra local files or environment variables when you want one of
these features:

- JIRA-aware test behavior through `pytest-jira`
- AI-powered enrichment of failed JUnit reports
- Copy-offload credential overrides for storage and ESXi access

> **Note:** The repository already ignores the most important local-secret files: `.providers.json`, `jira.cfg`, `.env`, and `junit-report.xml`.

## JIRA Integration

JIRA support ships as a dependency, but it is off by default. `pytest-jira` is declared in `pyproject.toml` and registers a `--jira` flag whose default is `False`. That flag is
not part of `addopts` in `pytest.ini`, so nothing JIRA-related happens unless you ask for it.

To connect the plugin to your JIRA instance, use the shipped template in `jira.cfg.example` and create a local `jira.cfg` with the same shape:

```ini
[DEFAULT]
url = <Jira URL>
token = <User Token>
```

Then enable the plugin per run:

```bash
uv run pytest -m warm --jira --tc=source_provider:ovirt-4.4.9 --tc=storage_class:my-block-storageclass
```

At the moment no test class in `tests/` carries a `pytest.mark.jira` marker, so `--jira` changes nothing until a test adds one.

> **Tip:** Keep `jira.cfg` local, or generate it at job runtime from your CI secret store. The repo already ignores `jira.cfg`, so there is no reason to commit a token.

## AI Failure Analysis

AI failure analysis is opt-in. It only activates when you pass `--analyze-with-ai`.

From `conftest.py`:

```python
analyze_with_ai_group = parser.getgroup(name="Analyze with AI")
analyze_with_ai_group.addoption("--analyze-with-ai", action="store_true", help="Analyze test failures using AI")
```

When that flag is present, the suite loads `.env`, then checks for the rootcoz server URL. It does not invent a provider or model: those defaults belong to the service and live
in `.rootcoz/settings.json`.

From `utilities/pytest_utils.py`:

```63:84:utilities/pytest_utils.py
def setup_ai_analysis(session: pytest.Session) -> None:
    """Configure AI analysis for test failure reporting.

    Loads environment variables and validates prerequisites. Disables AI analysis
    if ROOTCOZ_SERVER_URL is missing or if pytest was invoked with --collectonly
    or --setupplan. Provider/model defaults live in `.rootcoz/settings.json` and
    are applied by rootcoz, not by pytest.

    Args:
        session (pytest.Session): The pytest session object.
    """
    if is_dry_run(session.config):
        session.config.option.analyze_with_ai = False
        return

    load_dotenv()

    LOGGER.info("Setting up AI-powered test failure analysis")

    if not os.environ.get("ROOTCOZ_SERVER_URL"):
        LOGGER.warning("ROOTCOZ_SERVER_URL is not set. Analyze with AI features will be disabled.")
        session.config.option.analyze_with_ai = False
```

The environment variables are:

| Variable | Required | Default in code | What it controls |
| --- | --- | --- | --- |
| `ROOTCOZ_SERVER_URL` | Yes | none | Base URL of the rootcoz service |
| `ROOTCOZ_TIMEOUT` | No | `600` | HTTP timeout in seconds for the analysis request. Invalid values fall back to `600` |
| `ROOTCOZ_AI_PROVIDER` | No | none | Provider name sent to rootcoz. When unset, rootcoz applies `.rootcoz/settings.json` |
| `ROOTCOZ_AI_MODEL` | No | none | Model name sent to rootcoz. When unset, rootcoz applies `.rootcoz/settings.json` |

The suite already writes a JUnit report by default because `pytest.ini` sets `--junit-xml=junit-report.xml`. When there are failures, the AI integration reads that XML, posts it
to the rootcoz service, and writes the enriched XML back to the same file.

From `utilities/pytest_utils.py`:

```485:505:utilities/pytest_utils.py
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
```

A few practical details matter here:

- If `ROOTCOZ_SERVER_URL` is missing, the feature disables itself with a warning.
- If `ROOTCOZ_TIMEOUT` is invalid, the code falls back to `600`.
- Dry-run modes such as `--collect-only` and `--setup-plan` disable the feature.
- Successful runs skip enrichment because there are no failures to analyze.
- If the JUnit XML path is unset or the file is missing, enrichment is skipped.
- If enrichment fails, the original JUnit XML is preserved.

> **Warning:** The AI path sends the raw JUnit XML to `${ROOTCOZ_SERVER_URL}/analyze-failures`. That report can contain test names, failure messages, resource names, and any
> other details included in the report. Only enable this against a service you trust.

> **Tip:** Because `.env` is gitignored and only loaded when `--analyze-with-ai` is enabled, it is a good place for local `ROOTCOZ_*` settings.

## Cluster Credential Environment Variables

Cluster access is resolved from `tests/tests_config/config.py` first, then from the environment:

| Variable | Equivalent `--tc` key | Notes |
| --- | --- | --- |
| `CLUSTER_HOST` | `cluster_host` | Cluster API URL |
| `CLUSTER_USERNAME` | `cluster_username` | Cluster user |
| `CLUSTER_PASSWORD` | `cluster_password` | Cluster password |
| `CLUSTER_VERIFY_SSL` | none | Overrides `insecure_verify_skip`. Semantics are inverted: `true` means `insecure_verify_skip=False` |
| `PROVIDERS_JSON_PATH` | none | Path to the providers JSON file. Overridden by `--providers-json` |

Passing a credential through the environment keeps it out of the process argument list, which is why the repository's OpenShift Job template wires the first three through a
Secret.

## Copy-Offload Credential Overrides

Copy-offload is the most secret-heavy optional path in the repository. It is also the strictest one: the fixture fails early if required copy-offload configuration is missing.

The base configuration lives under the source provider entry in `.providers.json`, which the suite locates through `--providers-json`, `PROVIDERS_JSON_PATH`, or the default
`.providers.json` path.

A relevant excerpt from `.providers.json.example` shows the expected shape:

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

      # Optional: vSphere resource pool for VM placement during cloning
      # Priority order for resource pool selection:
      #   1. This configured value (highest priority - explicit override)
      #   2. Target ESXi host's pool (when esxi_host is set - automatic compatibility)
      #   3. Source VM/template's pool (preserve original location)
      #   4. Cluster-wide search with datastore compatibility check (fallback)
      # Example: "e2e-qe-resources" or leave commented to use automatic selection
      # "resource_pool": "<VSPHERE RESOURCE POOL NAME>",

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

> **Warning:** The comments in `.providers.json.example` are not valid JSON. The real `.providers.json` file is parsed with `json.loads(...)`, so remove the `# pragma:
> allowlist secret` comments when creating your own file.

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

Only some vendors need extra vendor-specific secret values:

| `storage_vendor_product` | Required vendor fields | Secret keys added |
| --- | --- | --- |
| `ontap` | `ontap_svm` | `ONTAP_SVM` |
| `vantara` | `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list` | `STORAGE_ID`, `STORAGE_PORT`, `HOSTGROUP_ID_LIST` |
| `primera3par` | none | none |
| `pureFlashArray` | `pure_cluster_prefix` | `PURE_CLUSTER_PREFIX` |
| `powerflex` | `powerflex_system_id` | `POWERFLEX_SYSTEM_ID` |
| `powermax` | `powermax_symmetrix_id` | `POWERMAX_SYMMETRIX_ID` |
| `powerstore` | none | none |
| `infinibox` | none | none |
| `flashsystem` | none | none |

> **Warning:** Not every `copyoffload` key is overrideable. In the current code paths, `storage_vendor_product`, `datastore_id`, `secondary_datastore_id`,
> `non_xcopy_datastore_id`, `rdm_lun_uuid`, `esxi_clone_method`, `resource_pool`, `default_vm_name`, and `dedicated_migration_hosts` are read directly from `.providers.json`.

> **Tip:** A good working pattern is to keep stable, non-secret facts in `.providers.json` and move only the sensitive pieces, such as passwords and vendor credentials, into
> `COPYOFFLOAD_*` environment variables.

### Extra Secret Keys

`storage_secret_extra` adds arbitrary string keys to the storage Secret `stringData`, for vendor settings that have no dedicated field. Keys must be non-empty strings and should
match the Secret names the Forklift populator expects, such as `POWERMAX_PORT_GROUP_NAME` or `STORAGE_SKIP_SSL_VERIFICATION`.

`COPYOFFLOAD_STORAGE_SECRET_EXTRA` overrides it. The variable holds a JSON object:

```bash
export COPYOFFLOAD_STORAGE_SECRET_EXTRA='{"POWERMAX_PORT_GROUP_NAME": "mtv_group_pg", "STORAGE_SKIP_SSL_VERIFICATION": "true"}'
```

Values from `.providers.json` are applied first, and the environment object replaces matching keys. A malformed value raises, because the code requires a JSON object with
non-empty string keys.

### How Those Values Become Runtime Secrets

The copy-offload fixture turns the resolved values into a Kubernetes `Secret`. Every vendor gets the same three base keys:

```python
secret_data: dict[str, str] = {
    "STORAGE_HOSTNAME": storage_hostname,
    "STORAGE_USERNAME": storage_username,
    "STORAGE_PASSWORD": storage_password,
}
```

Vendor-specific and extra keys are merged in afterwards. That secret is then referenced from the StorageMap config used by the copy-offload tests.

From `tests/copyoffload/test_copyoffload_migration.py`:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
    }
}
```

This is why environment overrides are useful: they let you keep the StorageMap logic unchanged while changing only the secret material injected into the run.

If you use SSH-based ESXi cloning, the vSphere provider also patches the provider setting to `esxiCloneMethod: ssh`. If you omit `esxi_clone_method` or leave it as `vib`, the
code treats `vib` as the default and does not patch anything.

## Secret Scanning

Two repository files exist specifically to keep secret scanning useful:

- `.providers.json.example` annotates placeholder credentials with `# pragma: allowlist secret`, which tells gitleaks those values are intentional placeholders.
  `.gitleaksignore` suppresses `generic-api-key` findings in generated documentation artifacts such as `docs/llms-full.txt` and `docs/search-index.json`, plus a few documentation
  pages whose command examples resemble keys.

> **Note:** The allowlist annotations live in the template, not in `.providers.json`. When you write your own provider file, drop the comments entirely.

## Handling Sensitive Values In Practice

The safest way to work with this repository is to separate stable configuration from secrets:

- Keep stable settings in `.providers.json`: provider type, version, datastore IDs, vendor selection, VM names, and clone method.
- Keep tokens and passwords in `jira.cfg`, `.env`, or `COPYOFFLOAD_*` environment variables.
- In automation, create those files at runtime from your CI or cluster secret store instead of baking them into images or checking them into Git.
- Treat `junit-report.xml` as sensitive if it may contain failure details you would not want to share broadly, especially when AI analysis is enabled.

The OpenShift Job guidance in `guides/copyoffload/how-to-run-copyoffload-tests.md` already demonstrates a good secret-injection pattern for automation:

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

```168:171:utilities/ssh_utils.py
        cmd_str = " ".join(cmd)
        if self.ocp_token:
            cmd_str = cmd_str.replace(self.ocp_token, "[REDACTED]")
        LOGGER.info(f"Full virtctl command: {cmd_str}")
```

> **Note:** That masking is useful, but it is not a guarantee that every secret in every code path will be redacted automatically.

> **Warning:** Copy the keys from `.providers.json.example`, not the comments. The `# pragma: allowlist secret` annotations are there for repository scanning and will break a
> real JSON file.

> **Tip:** For day-to-day use, a practical split is:
> keep `.providers.json` for non-secret structure,
> keep `jira.cfg` and `.env` local,
> and use `COPYOFFLOAD_*` or your CI secret manager for the values you would least want to store on disk.
