# Installation And Local Setup

`mtv-api-tests` is a live `pytest` suite for Migration Toolkit for Virtualization (MTV). A real local setup needs more than a virtual environment: you also need access to an OpenShift cluster, MTV and OpenShift Virtualization installed on that cluster, and at least one source provider defined in a local `.providers.json` file.

> **Warning:** A normal `pytest` run is not a mock or unit-test workflow. The suite talks to live providers and creates cluster resources such as namespaces, secrets, providers, plans, network maps, storage maps, and virtual machines.

## Python and `uv`

The project metadata allows Python 3.12 through 3.13, and the project image pins Python 3.12. For the least surprising local experience, use Python 3.12 and install dependencies with `uv`.

```toml
[project]
requires-python = ">=3.12, <3.14"
name = "mtv-api-tests"
version = "2.8.3"
description = "MTV API Tests"
```

The container build also makes the intended local setup clear:

```dockerfile
ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_CACHE_DIR=${APP_DIR}/.cache
```

From the repository root, install the locked environment with:

```bash
uv sync --locked
```

`uv` is the source of truth here. The repository ships `pyproject.toml` and `uv.lock`; it does not use a `requirements.txt` workflow.

> **Note:** If you plan to run the repository's pre-commit hooks as well, `.pre-commit-config.yaml` sets the hook interpreter to `python3.13`. The test environment itself still supports `>=3.12, <3.14`.

## Native packages on Linux

If `uv sync` needs to build any dependencies locally, the container image shows the system packages the project expects on a Fedora-based system:

```dockerfile
RUN dnf -y install \
  libxml2-devel \
  libcurl-devel \
  openssl \
  openssl-devel \
  libcurl-devel \
  gcc \
  clang \
  python3-devel \
```

On other distributions, install the equivalent SSL, XML, compiler, and Python development packages.

> **Tip:** You may not need all of these locally if `uv` can use prebuilt wheels, but they are the best reference for what the project image installs.

## Run from the repository root

The provider loader looks for `.providers.json` as a relative path:

```python
def load_source_providers() -> dict[str, dict[str, Any]]:
    """Load source providers from .providers.json.

    Returns:
        dict[str, dict[str, Any]]: Provider configurations keyed by provider name.
    """
    providers_file = Path(".providers.json")
```

That means your working directory matters.

> **Warning:** Run `uv` and `pytest` from the repository root. If you start from another directory, `.providers.json` can appear “missing” even when the file exists.

## How local configuration is loaded

`pytest` is already wired to `pytest-testconfig`, and the default config file is part of the repo:

```ini
addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
```

The default values in `tests/tests_config/config.py` are a starting point, not a complete local setup:

```python
insecure_verify_skip: str = "true"
source_provider_insecure_skip_verify: str = "false"
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace_prefix: str = "auto"
mtv_namespace: str = "openshift-mtv"
vm_name_search_pattern: str = ""
remote_ocp_cluster: str = ""
```

In practice, the most important runtime values are:

- `cluster_host`, `cluster_username`, and `cluster_password` for the OpenShift client
- `source_provider` to select the provider entry from `.providers.json`
- `storage_class` for the destination storage class
- `mtv_namespace` if your MTV operator is not installed in `openshift-mtv`
- `remote_ocp_cluster` only if you plan to run tests marked for remote-cluster scenarios

The repository’s own invocation examples pass those values with `--tc=` overrides:

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

For local shell usage, pass the same `--tc=` keys directly or expand them from your shell environment.

> **Tip:** The example above avoids typing the cluster password literally on the command line. Reusing that pattern is a good idea for local runs too.

> **Note:** A `.env` file is only auto-loaded for the optional `--analyze-with-ai` path. Standard cluster and provider configuration still comes from `.providers.json` and `--tc=` values.

## Create `.providers.json`

The suite expects a file named `.providers.json` in the repository root. Start from `.providers.json.example`, then replace the placeholders with real values.

A typical vSphere entry looks like this in the example file:

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
},
```

The example file also includes provider templates for `ovirt`, `openstack`, `openshift`, and `ova`.

A few important details matter here:

- The top-level key is what `source_provider` selects. If you run with `--tc=source_provider:vsphere-copy-offload`, your `.providers.json` file must contain a top-level key with that exact name.
- Guest OS credentials are not optional decoration. Post-migration checks read `guest_vm_linux_user` and `guest_vm_linux_password`, or the Windows equivalents, from the provider config.
- The OpenShift provider example intentionally leaves connection fields blank. When the provider type is `openshift`, the code reuses the current cluster connection and cluster secret instead of building a completely separate provider secret.

> **Note:** `.providers.json.example` contains comments such as `# pragma: allowlist secret`. Those comments are useful in the example file, but they are not valid JSON. Your real `.providers.json` must be valid JSON.

> **Tip:** `.providers.json` is already listed in `.gitignore`, so keep real credentials there instead of checking them into the repository.

### Copy-offload overrides

If you plan to run copy-offload tests, the helper code checks environment variables before reading the `copyoffload` block from `.providers.json`:

```python
env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
return os.getenv(env_var_name) or copyoffload_config.get(credential_name)
```

That means variables such as these override file values when present:

- `COPYOFFLOAD_STORAGE_HOSTNAME`
- `COPYOFFLOAD_STORAGE_USERNAME`
- `COPYOFFLOAD_STORAGE_PASSWORD`
- `COPYOFFLOAD_ESXI_HOST`
- `COPYOFFLOAD_ESXI_USER`
- `COPYOFFLOAD_ESXI_PASSWORD`

> **Tip:** Using `COPYOFFLOAD_*` environment variables is a good way to keep storage-array and ESXi credentials out of `.providers.json`.

## Prepare the cluster and source VMs

A working Python environment is only half of the setup. Before a real test run, your lab should also be ready:

- MTV must already be installed, and its `forklift-*` pods must be running in the namespace configured by `mtv_namespace` (default: `openshift-mtv`)
- Your OpenShift user must be able to create and clean up the resources the suite manages
- Your source provider must actually contain VMs or templates that match the names used by the selected test plans
- If a test expects `guest_agent: True`, the source VM should have a working guest agent

The test plans are data-driven. For example, one of the built-in sanity plans looks like this:

```python
tests_params: dict = {
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

That snippet tells you exactly what the lab needs for that scenario:

- A source VM named `mtv-tests-rhel8`
- The ability to power it on before migration
- A guest agent available inside the VM

More advanced plans in `tests/tests_config/config.py` add things like custom VM target namespaces, Multus networks, node selectors, labels, and copy-offload requirements.

> **Tip:** For a fresh lab, start by aligning your environment with the simple sanity plans before trying comprehensive or copy-offload scenarios.

> **Note:** Tests marked for remote-cluster scenarios are designed to use `remote_ocp_cluster`. Leave that value empty unless you actually have a remote-cluster setup.

## Validate the installation safely

Before launching a real migration run, it is a good idea to do a dry validation first. `tox.toml` uses these commands for its lightweight pytest checks:

```bash
uv run pytest --setup-plan
uv run pytest --collect-only
```

These are useful first checks after `uv sync --locked` because they validate importability and test collection without running the live migrations themselves.

> **Warning:** `uv run pytest` without a dry-run flag is a live infrastructure run.

## How `virtctl` is discovered or downloaded

Most users do not need to install `virtctl` manually. The session-scoped setup code makes sure it is available.

The first step is to reuse an existing binary if one is already present:

```python
# Check if already available
existing = _check_existing_virtctl(download_dir)
if existing:
    add_to_path(str(existing.parent))
    return existing

LOGGER.info("virtctl not found, downloading from cluster...")

# Get ConsoleCLIDownload resource
console_cli_download = ConsoleCLIDownload(
    client=client,
    name="virtctl-clidownloads-kubevirt-hyperconverged",
    ensure_exists=True,
)
```

`virtctl` is needed because VM SSH access is implemented through `virtctl port-forward`:

```python
cmd = [
    virtctl_path,
    "port-forward",
    f"vm/{self.vm.name}",
    f"{local_port}:22",
    "--namespace",
    self.vm.namespace,
    "--address",
    "127.0.0.1",
]
```

In practice, the `virtctl` flow works like this:

- If `virtctl` is already on `PATH`, the suite reuses it
- If not, it checks a shared cache under the system temp directory
- If there is no cached binary, it reads the cluster `ConsoleCLIDownload` named `virtctl-clidownloads-kubevirt-hyperconverged`
- It picks the download URL that matches the local host OS and architecture
- It downloads the archive, extracts the `virtctl` binary, makes it executable, and prepends its directory to `PATH`

The auto-download logic currently covers:

- Linux and macOS hosts
- `x86_64`, `aarch64`, and `arm64` architectures

The session fixture also caches `virtctl` by cluster version and guards the download with a file lock so parallel `pytest-xdist` workers do not all fetch the same binary at once.

> **Warning:** Windows hosts are not covered by the current `virtctl` auto-detection logic. The downloader only maps `linux` and `darwin`.

> **Tip:** If you want to force a fresh `virtctl` download, remove the cached `pytest-shared-virtctl/<cluster-version>` directory under your system temp directory, or place a different `virtctl` earlier in `PATH`.

## Optional but useful local tools

`oc` is not how the suite discovers `virtctl`, and it is not required just to create the Python environment. It is still a good tool to have locally because the default failure-data path can run `oc adm must-gather` when tests fail.

> **Tip:** If you do not want automatic failure data collection, start pytest with `--skip-data-collector`. Otherwise the default behavior may try to collect `.data-collector` output and run must-gather on failures.
