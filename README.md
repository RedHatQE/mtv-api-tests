# mtv-api-tests

## Source providers

File `.providers.json` in the root directory of the repository with the source providers data

### Provider Requirements

Each source provider requires pre-existing base VMs or templates for test execution:

- **VMware vSphere**: Base VM must exist (e.g., `mtv-tests-rhel8`)
  - Tests will clone from this base VM for migration testing
  - VM should be powered off and in a ready state

- **OpenStack**: Base VM/instance must exist (e.g., `mtv-tests-rhel8`)
  - Tests will clone from this base instance using snapshots
  - Instance should be in ACTIVE or SHUTOFF state

- **RHV/oVirt**: Template must exist (e.g., `mtv-tests-rhel8`)
  - Tests will create VMs from this template
  - Template should have sufficient memory (minimum 1536 MiB recommended)
  - Ensure template's "Physical Memory Guaranteed" setting is not misconfigured

**Note**: The base VM/template names are referenced in test configurations. Ensure these resources exist in your
source provider before running tests.

## Prerequisites

Before running the test suite, ensure the following tools are installed and available in your PATH:

### Required Tools

1. **uv** - Python package manager
   - Install: [uv installation guide](https://github.com/astral-sh/uv)

2. **oc** - OpenShift CLI client
   - Ensure `oc` is in your PATH:

     ```bash
     export PATH="<oc path>:$PATH"
     ```

3. **virtctl** - Kubernetes virtualization CLI
   - Required for SSH connections to migrated VMs
   - Must be compatible with your target OpenShift cluster version
   - Installation options:
     - **From OpenShift cluster**: Download from the OpenShift web console under "Command Line Tools"
     - **From GitHub releases**: [kubevirt/kubevirt releases](https://github.com/kubevirt/kubevirt/releases)
   - Verify installation:

     ```bash
     virtctl version
     ```

### Setup

```bash
# Install dependencies
uv sync
```

Run openshift-python-wrapper in DEBUG (show the yamls requests)

```bash
export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG
```

## Update The Docker Image

```bash
docker build -f Dockerfile -t mtv-api-tests
docker login quay.io
docker push mtv-api-tests quay.io/openshift-cnv/mtv-tests:latest
```

## Running Tests with Container

**Note:** For Podman/SELinux (RHEL/Fedora), add `:z` to volume mounts: `-v $(pwd)/.providers.json:/app/.providers.json:ro,z`

```bash
docker run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -v $(pwd)/kubeconfig:/app/kubeconfig:ro \
  -e KUBECONFIG=/app/kubeconfig \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -s \
  --tc=cluster_host:https://api.example.cluster:6443 \
  --tc=cluster_username:kubeadmin \
  --tc=cluster_password:'YOUR_PASSWORD' \
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=storage_class:standard-csi \
  --tc=target_ocp_version:4.18

# Example with full configuration
docker run --rm \
  -v .providers.json:/app/.providers.json:ro \
  -v jira.cfg:/app/jira.cfg:ro \
  -v kubeconfig:/app/kubeconfig:ro \
  -e KUBECONFIG=/app/kubeconfig \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -s \
  --tc=cluster_host:https://api.example.cluster:6443 \
  --tc=cluster_username:kubeadmin \
  --tc=cluster_password:'YOUR_PASSWORD' \
  --tc=target_ocp_version:4.20 \
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=target_namespace:mtv-api-tests-vmware8 \
  --tc=storage_class:standard-csi \
  --tc=release_test:true \
  --skip-data-collector
```

### Required Files

- `.providers.json`: Source provider configurations
- `jira.cfg`: Jira configuration file
- `kubeconfig`: Kubernetes cluster access

### Common Test Configuration Parameters

- `--tc=cluster_host`: OpenShift API URL (e.g., <https://api.example.cluster:6443>) [required]
- `--tc=cluster_username`: Cluster username (e.g., kubeadmin) [required]
- `--tc=cluster_password`: Cluster password [required]
- `--tc=source_provider_type`: vsphere, rhv, openstack, etc. [required]
- `--tc=source_provider_version`: Provider version (6.5, 7.0.3, 8.0.1) [required]
- `--tc=storage_class`: Storage class for testing [required]
- `--tc=target_ocp_version`: Target OpenShift version (e.g., 4.18) [required]
- `--tc=target_namespace`: Namespace for test resources [optional]

#### Authentication notes

- These three options are required for the test suite to authenticate to the cluster via API.
- Keep the kubeconfig mount and KUBECONFIG env in container runs so oc adm must-gather can execute.
- Quote passwords with special characters. Prefer passing secrets via environment variables to avoid shell history exposure.

```bash
export CLUSTER_HOST=https://api.example.cluster:6443
export CLUSTER_USERNAME=kubeadmin
export CLUSTER_PASSWORD='your-password'
uv run pytest -s \
  --tc=cluster_host:"$CLUSTER_HOST" \
  --tc=cluster_username:"$CLUSTER_USERNAME" \
  --tc=cluster_password:"$CLUSTER_PASSWORD" \
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=storage_class:standard-csi
```

## Pytest

```bash
# Local run example
uv run pytest -s \
  --tc=cluster_host:https://api.example.cluster:6443 \
  --tc=cluster_username:kubeadmin \
  --tc=cluster_password:'YOUR_PASSWORD' \
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=storage_class:standard-csi \
  --tc=target_ocp_version:4.18
```

Set log collector folder: (default to `/tmp/mtv-api-tests`)

```bash
uv run pytest .... --data-collector-path <path to log collector folder>
```

After run there is `resources.json` file under `--data-collector-path` that hold all created resources during the run.
To delete all created resources using the above file run:

```bash
uv run tools/clean_cluster.py <path-to-resources.json>
```

Run without data-collector:

```bash
uv run pytest .... --skip-data-collector
```

## Run options

Run without calling teardown (Do not delete created resources)

```bash
uv run pytest --skip-teardown
```

## Adding New Tests

### Step 1: Define Test Parameters

Add your test configuration to `tests_params` in `tests/tests_config/config.py`:

```python
tests_params: dict = {
    # ... existing tests
    "test_your_new_test": {
        "virtual_machines": [
            {
                "name": "vm-name-for-test",
                "source_vm_power": "on",  # "on" for warm, "off" for cold
                "guest_agent": True,
                "target_power_state": "on",  # Optional: "on" or "off" - destination VM power state after migration
            },
        ],
        "warm_migration": True,  # True for warm, False for cold
        "preserve_static_ips": True, # True for preserving source Vm's Static IP
        # pvc_name_template to set Forklift PVC Name template, supports Go template syntax: {{.FileName}},
        # {{.DiskIndex}}, {{.VmName}} and  Sprig functions, i.e.:
        "pvc_name_template": '{{ .FileName | trimSuffix \".vmdk\" | replace \"_\" \"-\" }}-{{.DiskIndex}}',
        "pvc_name_template_use_generate_name": False,  # Boolean to control template usage  
    },
}
```

### Step 2: Create Test Function

```python
import pytest
from pytest_testconfig import py_config

@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_your_new_test"],
            py_config["tests_params"]["test_your_new_test"],
        )
    ],
    indirect=True,
    ids=["descriptive-id"],
)
def test_your_new_test(request, fixture_store, ...):
    # Your test implementation
```

### Custom Configuration

You can create your own config file and use it with:

```python
# your_config.py
cluster_host = "https://api.example.cluster:6443"
cluster_username = "kubeadmin"
cluster_password = "YOUR_PASSWORD"
```

Usage remains the same:

```bash
uv run pytest --tc-file=your_config.py
```

## Run Functional Tests tier1

```bash
uv run pytest -m tier1 \
  --tc=cluster_host:https://api.example.cluster:6443 \
  --tc=cluster_username:kubeadmin \
  --tc=cluster_password:'YOUR_PASSWORD' \
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=storage_class:<storage_class> \
  --tc=target_ocp_version:4.18
```

## Run Copy-Offload Tests

Copy-offload tests leverage shared storage for faster migrations. Add `copyoffload` config to `.providers.json`
and ensure template VM has QEMU guest agent installed.

The `esxi_clone_method` allows specifying `ssh` to perform disk cloning directly on the ESXi host,
as an alternative to the default VIB-based method. This requires providing ESXi host credentials.

**Configuration in `.providers.json`:**
Add the `copyoffload` section under your vSphere provider configuration (see `.providers.json.example` for complete example):

```json
"copyoffload": {
  "storage_vendor_product": "ontap",
  "datastore_id": "datastore-123",
  "template_name": "rhel9-template",
  "storage_hostname": "storage.example.com",
  "storage_username": "admin",
  "storage_password": "password",
  "ontap_svm": "vserver-name",
  "esxi_clone_method": "ssh", # default 'vib'
  "esxi_host": "your-esxi-host.example.com",
  "esxi_user": "root",
  "esxi_password": "your-esxi-password"
}
```

**Vendor-specific fields:**

- NetApp ONTAP: `ontap_svm` (SVM name)
- Pure Storage: `pure_cluster_prefix`
- PowerMax: `powermax_symmetrix_id`
- PowerFlex: `powerflex_system_id`

**Security Note:** For development/testing, credentials can be stored in `.providers.json`.
For production/CI, use environment variables to override sensitive values without modifying config files:

```bash
# Optional: Override credentials with environment variables (overrides .providers.json)
export COPYOFFLOAD_STORAGE_HOSTNAME=storage.example.com
export COPYOFFLOAD_STORAGE_USERNAME=admin
export COPYOFFLOAD_STORAGE_PASSWORD=secretpassword
export COPYOFFLOAD_ONTAP_SVM=vserver-name  # For NetApp ONTAP only
```

If credentials are already in `.providers.json`, environment variables are not required.

**Run the tests:**

```bash
uv run pytest -m copyoffload \
  --tc=cluster_host:https://api.example.cluster:6443 \
  --tc=cluster_username:kubeadmin \
  --tc=cluster_password:'YOUR_PASSWORD' \
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.3.00400 \
  --tc=storage_class:rhosqe-ontap-san-block \
  --tc=target_ocp_version:4.18
```

## Release new version

### requirements

- Export GitHub token

```bash
export GITHUB_TOKEN=<your_github_token>
```

- [release-it](https://github.com/release-it/release-it)

```bash
sudo npm install --global release-it
npm install --save-dev @release-it/bumper
```

### usage

- Create a release, run from the relevant branch.
  To create a release, run:

```bash
git main
git pull
release-it # Follow the instructions

```
