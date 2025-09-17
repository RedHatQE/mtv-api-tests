# mtv-api-tests

## Source providers

File `.providers.json` in the root directory of the repository with the source providers data

install [uv](https://github.com/astral-sh/uv)

```bash
uv sync

# make sure oc client path in $PATH
export PATH="<oc path>:$PATH"

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

### Container Test Execution

```bash
# Basic container test run
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
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=storage_class:standard-csi

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
- `--tc=source_provider_type`: vsphere, rhv, openstack, etc.
- `--tc=source_provider_version`: Provider version (6.5, 7.0.3, 8.0.1)
- `--tc=storage_class`: Storage class for testing
- `--tc=target_namespace`: Namespace for test resources
- `--tc=target_ocp_version`: Target OpenShift version

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
  --tc=storage_class:standard-csi
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
            },
        ],
        "warm_migration": True,  # True for warm, False for cold
    },
}
```

### Step 2: Create Test Function

```python
import pytest
from pytest_testconfig import py_config

@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_your_new_test"])],
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
  --tc=storage_class:<storage_class>
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
