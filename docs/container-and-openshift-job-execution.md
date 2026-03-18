# Container And OpenShift Job Execution

Running `mtv-api-tests` from a container or an OpenShift `Job` gives you a repeatable environment for long migration runs, shared execution in CI-style workflows, and predictable artifact collection. The key is understanding what the image expects at runtime:

- a `.providers.json` file in the container working directory
- pytest testconfig values for the OpenShift cluster and test selection
- an overridden container command, because the image defaults to collection only

## How The Image Starts

The checked-in image is built to run from `/app`, and its default command only collects tests:

```dockerfile
ARG APP_DIR=/app
WORKDIR ${APP_DIR}

CMD ["uv", "run", "pytest", "--collect-only"]
```

Pytest is also preconfigured in the repo:

```ini
[pytest]
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

> **Note:** A plain `podman run ... ghcr.io/redhatqe/mtv-api-tests:latest` will not execute migrations. Override the command with `uv run pytest ...`.

The suite reads provider definitions from `.providers.json` and builds the OpenShift client from pytest config values:

```python
def load_source_providers() -> dict[str, dict[str, Any]]:
    providers_file = Path(".providers.json")
    if not providers_file.exists():
        return {}

def get_cluster_client() -> DynamicClient:
    host = get_value_from_py_config("cluster_host")
    username = get_value_from_py_config("cluster_username")
    password = get_value_from_py_config("cluster_password")
    insecure_verify_skip = get_value_from_py_config("insecure_verify_skip")
    _client = get_client(host=host, username=username, password=password, verify_ssl=not insecure_verify_skip)
```

In practice, every real run needs these values:

- `.providers.json`
- `source_provider`
- `storage_class`
- `cluster_host`
- `cluster_username`
- `cluster_password`

> **Warning:** The suite does not currently create its OpenShift client from the pod service account. Even inside an OpenShift `Job`, you still need to provide `cluster_host`, `cluster_username`, and `cluster_password`.

## Provider And Test Config

### `.providers.json`

The provider file lives at `.providers.json` in the working directory, so inside the image the simplest path is `/app/.providers.json`.

A real example from `.providers.json.example`:

```json
{
  "vsphere": {
    "type": "vsphere",
    "version": "<SERVER VERSION>",
    "fqdn": "SERVER FQDN/IP",
    "api_url": "<SERVER FQDN/IP>/sdk",
    "username": "USERNAME",
    "password": "PASSWORD",
    "guest_vm_linux_user": "LINUX VMS USERNAME",
    "guest_vm_linux_password": "LINUX VMS PASSWORD",
    "guest_vm_win_user": "WINDOWS VMS USERNAME",
    "guest_vm_win_password": "WINDOWS VMS PASSWORD",
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
  }
}
```

The top-level key is the provider name you pass through pytest. If your file uses `"vsphere"`, then your run must include `--tc=source_provider:vsphere`.

> **Tip:** The top-level key can be descriptive, versioned, or site-specific. What matters is that `source_provider` matches it exactly.

### `tests/tests_config/config.py`

The repo already ships a Python testconfig file with global defaults and `tests_params` used by the test modules:

```python
insecure_verify_skip: str = "true"
source_provider_insecure_skip_verify: str = "false"
target_namespace_prefix: str = "auto"
mtv_namespace: str = "openshift-mtv"
plan_wait_timeout: int = 3600

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

Most users should keep the checked-in `config.py` and supply runtime values with `--tc=...`.

If you do mount your own replacement config file, start from `tests/tests_config/config.py` instead of creating an empty file. The tests rely on `tests_params` being present.

## Running From The Container Image

Examples below use `podman`, but the same pattern works with Docker.

A practical local run mounts `.providers.json`, exports cluster credentials into the container, and overrides the image command:

```bash
export CLUSTER_HOST="https://api.example.com:6443"
export CLUSTER_USERNAME="kubeadmin"
export CLUSTER_PASSWORD="<password>"

podman run --rm \
  -e CLUSTER_HOST \
  -e CLUSTER_USERNAME \
  -e CLUSTER_PASSWORD \
  -v "$(pwd)/.providers.json:/app/.providers.json:ro" \
  -v "$(pwd)/results:/app/results" \
  ghcr.io/redhatqe/mtv-api-tests:latest \
  /bin/bash -c 'uv run pytest -m tier0 \
    -v \
    ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
    ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
    ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
    --tc=source_provider:vsphere \
    --tc=storage_class:my-block-storageclass \
    --junit-xml=/app/results/junit-report.xml \
    --log-file=/app/results/pytest-tests.log \
    --data-collector-path=/app/results/data'
```

Useful marker selections come directly from `pytest.ini` and the test modules:

- `-m tier0` for smoke-style coverage
- `-m warm` for warm migration coverage
- `-m copyoffload` for copy-offload coverage
- `-m remote` for remote-cluster scenarios when `remote_ocp_cluster` is configured

> **Note:** `warm` is not universally supported for every provider type. If you select `-m warm` against a provider that the warm test module skips, pytest will report skips rather than failures.

If you prefer a mounted config file instead of many `--tc=` flags, mount your own Python config and point pytest at it:

```bash
uv run pytest --tc-file=/app/config.py --tc-format=python ...
```

That is only safe when `/app/config.py` is based on the repo’s existing `tests/tests_config/config.py`.

## Running Inside An OpenShift Job

The repo already contains a checked-in `Job` pattern under `docs/copyoffload/`. It is written for copy-offload, but the wiring is the same for any suite: mount `.providers.json`, provide cluster credentials from a `Secret`, and expand them into `--tc=` arguments in the container command.

### 1. Create A Secret

```bash
oc create namespace mtv-tests

read -sp "Enter cluster password: " CLUSTER_PASSWORD && echo
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  --from-literal=cluster_host=https://api.your-cluster.com:6443 \
  --from-literal=cluster_username=kubeadmin \
  --from-literal=cluster_password="${CLUSTER_PASSWORD}" \
  -n mtv-tests
unset CLUSTER_PASSWORD
```

### 2. Create The Job

This example is copied from the checked-in documentation:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: mtv-copyoffload-tests
  namespace: mtv-tests
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: tests
        image: ghcr.io/redhatqe/mtv-api-tests:latest
        env:
        - name: CLUSTER_HOST
          valueFrom:
            secretKeyRef:
              name: mtv-test-config
              key: cluster_host
              optional: true
        - name: CLUSTER_USERNAME
          valueFrom:
            secretKeyRef:
              name: mtv-test-config
              key: cluster_username
              optional: true
        - name: CLUSTER_PASSWORD
          valueFrom:
            secretKeyRef:
              name: mtv-test-config
              key: cluster_password
              optional: true
        command:
          - /bin/bash
          - -c
          - |
            uv run pytest -m copyoffload \
              -v \
              ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
              ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
              ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
              --tc=source_provider:vsphere-8.0.3.00400 \
              --tc=storage_class:my-block-storageclass
        volumeMounts:
        - name: config
          mountPath: /app/.providers.json
          subPath: providers.json
      volumes:
      - name: config
        secret:
          secretName: mtv-test-config
```

To reuse this pattern for other suites:

- change `-m copyoffload` to `-m tier0` for smoke tests
- change `-m copyoffload` to `-m warm` for warm migration runs
- replace `vsphere-8.0.3.00400` with your actual `.providers.json` key
- replace `my-block-storageclass` with the storage class used by your target cluster
- replace the image if you built and pushed your own copy

> **Warning:** `CLUSTER_HOST`, `CLUSTER_USERNAME`, and `CLUSTER_PASSWORD` are not read directly by the test code. In this pattern they exist only so the shell can expand them into `--tc=` options.

## Copy-Offload Credentials

Copy-offload is the one area where the suite can read credentials directly from environment variables as well as from `.providers.json`:

```python
def get_copyoffload_credential(
    credential_name: str,
    copyoffload_config: dict[str, Any],
) -> str | None:
    env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
    return os.getenv(env_var_name) or copyoffload_config.get(credential_name)
```

That means a `Job` can inject copy-offload storage credentials from a `Secret` without hard-coding them in `.providers.json`.

Common environment variable names come straight from the fixtures:

- `COPYOFFLOAD_STORAGE_HOSTNAME`
- `COPYOFFLOAD_STORAGE_USERNAME`
- `COPYOFFLOAD_STORAGE_PASSWORD`
- vendor-specific names such as `COPYOFFLOAD_ONTAP_SVM`
- ESXi SSH values such as `COPYOFFLOAD_ESXI_HOST`, `COPYOFFLOAD_ESXI_USER`, and `COPYOFFLOAD_ESXI_PASSWORD`

> **Note:** This environment-variable fallback applies to copy-offload storage credentials. It does not replace the normal `cluster_host`, `cluster_username`, `cluster_password`, or `source_provider` inputs.

## Collecting Results

By default, a run produces these useful artifacts inside `/app`:

- `junit-report.xml`
- `pytest-tests.log`
- `.data-collector/`

The data collector path is configurable, and the suite uses it for resource tracking and failure collection:

- default path: `.data-collector`
- change it with `--data-collector-path=/some/path`
- disable it with `--skip-data-collector`

When data collection is enabled, the suite writes `resources.json` there and can also collect must-gather data on failures.

For a finished OpenShift `Job`, the checked-in docs already show how to stream logs and copy the JUnit file out of the pod:

```bash
oc logs -n mtv-tests job/mtv-copyoffload-tests -f

POD_NAME=$(oc get pods -n mtv-tests -l job-name=mtv-copyoffload-tests -o jsonpath='{.items[0].metadata.name}')
oc cp mtv-tests/$POD_NAME:/app/junit-report.xml ./junit-report.xml
```

You can collect the other artifacts the same way:

```bash
oc cp mtv-tests/$POD_NAME:/app/pytest-tests.log ./pytest-tests.log
oc cp mtv-tests/$POD_NAME:/app/.data-collector ./data-collector
```

For container runs, the easiest pattern is to mount a results directory and redirect outputs into it with:

- `--junit-xml=/app/results/junit-report.xml`
- `--log-file=/app/results/pytest-tests.log`
- `--data-collector-path=/app/results/data`

> **Tip:** For long-running OpenShift Jobs, mount a PVC and write all artifacts to that volume. That avoids depending on the pod filesystem after completion.

## Cleanup And Debugging

The suite cleans up resources automatically unless you opt out. That includes created MTV resources and tracked VMs.

If you want to keep resources around for investigation, add:

```bash
--skip-teardown
```

If you skip teardown, keep the data collector output. The repo includes a cleanup helper that can use the saved `resources.json` file:

```bash
uv run tools/clean_cluster.py .data-collector/resources.json
```

`resources.json` is only created when data collection is enabled.

> **Warning:** If you pass `--skip-data-collector`, the suite will not write `.data-collector/resources.json`, and failed runs will not gather the extra collector output.

> **Note:** The completed `Job` pod remains available until you delete the `Job`, so you can still fetch logs and artifacts after the test run finishes.
