# MTV API Test Suite

Test suite for validating VM migrations to OpenShift from VMware vSphere,
RHV, OpenStack, and OVA using Migration Toolkit for Virtualization (MTV).

---

## Prerequisites

### Local Machine Requirements

- **OpenShift cluster** with MTV operator installed
- **Podman or Docker** - To run the test container
  - Linux/macOS: Podman or Docker
  - Windows: Docker Desktop or Podman Desktop

### Source Provider Requirements

You need a base VM/template in your source provider:

| Provider | Resource Type | Requirements |
| -------- | ------------- | ------------ |
| **VMware vSphere** | VM | Powered off, QEMU guest agent installed |
| **RHV/oVirt** | Template | Min 1536 MiB memory |
| **OpenStack** | Instance | ACTIVE/SHUTOFF state, QEMU guest agent installed |
| **OVA** ⚠️ Tech Preview | OVA file | NFS-accessible OVA files |

> **Note**: Copy-offload tests have additional prerequisites. See the
> [Copy-Offload Testing Guide](docs/how-to-run-copyoffload-tests.md) for details.

### Verify Setup

```bash
podman --version  # or: docker --version
```

**Optional** - If you have `oc` CLI installed, you can verify your cluster:

```bash
oc whoami                                # Check cluster access
oc get csv -n openshift-mtv | grep mtv  # Verify MTV operator
```

---

## Quick Start

### 1. Build and Push the Test Image

**Important**: A pre-built public image is available at `ghcr.io/redhatqe/mtv-api-tests:latest`. You can use it directly
or build and push your own custom image.

**Option A: Use the public image** (recommended):

Use `ghcr.io/redhatqe/mtv-api-tests:latest` directly in the commands below.

**Option B: Build your own custom image**:

```bash
# Clone the repository
git clone https://github.com/RedHatQE/mtv-api-tests.git
cd mtv-api-tests

# Build the image (use 'docker' if you prefer Docker)
podman build -t <YOUR-REGISTRY>/mtv-tests:latest .

# Push to your registry
podman push <YOUR-REGISTRY>/mtv-tests:latest
```

Replace `<YOUR-REGISTRY>` with your registry (e.g., `quay.io/youruser`, `docker.io/youruser`).

### 2. Grant Permissions

Ensure your OpenShift user has permissions to create MTV resources and VMs. If using `oc` CLI:

```bash
oc adm policy add-cluster-role-to-user cluster-admin $(oc whoami)
```

Or have your cluster admin grant the necessary permissions to the user account you'll use for testing.

### 3. Configure Your Source Provider

**What is `.providers.json`?** A configuration file that tells the tests how to connect to your source
virtualization platform.

**Why do you need it?** The tests need to:

- Connect to your source provider (vSphere, RHV, OpenStack, or OVA)
- Find the base VM to clone for testing
- Create test VMs and perform migrations

**What should it include?**

- Connection details (hostname, credentials)
- Location information (datacenter, cluster)
- Base VM/template name to use for testing

### Security Considerations

**Protect your credentials file:**

⚠️ **IMPORTANT**: The `.providers.json` file contains sensitive credentials. Follow these security practices:

- **Set restrictive permissions**: `chmod 600 .providers.json` (owner read/write only)
- **Never commit to Git**: Add `.providers.json` to your `.gitignore` file
- **Rotate secrets regularly**: Update passwords and credentials on a regular schedule
- **Use secret management**: For OpenShift deployments, use Kubernetes secrets
- **Delete when done**: Remove the file from local systems when no longer needed

**About `# pragma: allowlist secret` comments:**

> ⚠️ The JSON examples below contain `# pragma: allowlist secret` comments - these are **REQUIRED for this
> repository's pre-commit hooks** but are **NOT valid JSON**. Do NOT copy these comments to your actual
> `.providers.json` file. They exist only for documentation tooling, not security.

Create a `.providers.json` file in your current directory with your provider's details.

**VMware vSphere Example:**

```json
{
  "vsphere-8.0.1": {
    "type": "vsphere",
    "version": "8.0.1",
    "fqdn": "vcenter.example.com",
    "api_url": "https://vcenter.example.com/sdk",
    "username": "administrator@vsphere.local",
    "password": "your-password",  # pragma: allowlist secret
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password"  # pragma: allowlist secret
  }
}
```

**Key requirements:**

- Configuration key must follow pattern: `{type}-{version}` (e.g., `"vsphere-8.0.1"` for vSphere 8.0.1)
- Replace `8.0.1` with your actual vSphere version - both the key and `version` field must match
- All fields shown above are required
- Replace placeholder values with your actual credentials and endpoints

**For other providers** (RHV, OpenStack, OVA, or copy-offload configuration):

```bash
# Use the example file as a template
cp .providers.json.example .providers.json
# Edit with your actual values
```

See `.providers.json.example` for complete templates of all supported providers.

---

### 4. Find Your Storage Class

Check which storage classes are available in your OpenShift cluster:

```bash
oc get storageclass
```

Pick one that supports block storage (e.g., `ocs-storagecluster-ceph-rbd`, `ontap-san-block`).
You'll use this name in the next step.

### 5. Run Your First Test

Execute tier0 tests (smoke tests) using the containerized test suite:

```bash
podman run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  ghcr.io/redhatqe/mtv-api-tests:latest \
  uv run pytest -m tier0 -v \
    --tc=cluster_host:https://api.your-cluster.com:6443 \
    --tc=cluster_username:kubeadmin \
    --tc=cluster_password:'your-cluster-password' \  # pragma: allowlist secret
    --tc=source_provider_type:vsphere \
    --tc=source_provider_version:8.0.1 \
    --tc=storage_class:YOUR-STORAGE-CLASS
```

> **Note**: On RHEL/Fedora with SELinux, add `,z` to volume mounts:
> `-v $(pwd)/.providers.json:/app/.providers.json:ro,z`.
> You can use `docker` instead of `podman` if preferred.
>
> **Windows Users**: Replace `$(pwd)` with `${PWD}` in PowerShell or use absolute paths like
> `C:\path\to\.providers.json:/app/.providers.json:ro`. Requires Docker Desktop or Podman Desktop.

**Replace**:

- `https://api.your-cluster.com:6443` → Your OpenShift API URL
- `kubeadmin` → Your cluster username
- `your-cluster-password` → Your cluster password
- `YOUR-STORAGE-CLASS` → Your storage class from step 4
- `vsphere` → Provider type from your `.providers.json` key: `vsphere`, `ovirt`, `openstack`, or `ova`
- `8.0.1` → Provider version from your `.providers.json` key (must match exactly)

---

## Running Different Test Categories

The Quick Start runs **tier0** tests (smoke tests). You can run other test categories by changing the `-m` marker:

| Marker | What It Tests | When to Use |
| ------ | ------------- | ----------- |
| `tier0` | Smoke tests - critical paths | First run, quick validation |
| `copyoffload` | Fast migrations via shared storage | Testing storage arrays |
| `warm` | Warm migrations (VMs stay running) | Specific scenario testing |

**Examples** - Change `-m tier0` to run different tests:

> **Note**: In the examples below, replace `podman run ...` with the full `podman run` command shown in the
> Quick Start section (step 5), including the image name and volume mounts.

```bash
# Warm migration tests
podman run ... uv run pytest -m warm -v --tc=source_provider_type:vsphere ...

# Copy-offload tests
podman run ... uv run pytest -m copyoffload -v --tc=source_provider_type:vsphere ...

# Combine markers
podman run ... uv run pytest -m "tier0 or warm" -v --tc=source_provider_type:vsphere ...
```

---

## Running as OpenShift Job

For long-running test suites or automated CI/CD pipelines, you can run tests as OpenShift Jobs:

### Step 1: Create Secret with Configuration

Store your `.providers.json` file and optionally cluster credentials as an OpenShift secret:

**Option A: Providers configuration only** (credentials passed as command args in Job):

```bash
oc create namespace mtv-tests
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  -n mtv-tests
```

**Option B: Include cluster credentials in Secret** (recommended - avoids exposing secrets in Job YAML):

```bash
oc create namespace mtv-tests
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  --from-literal=cluster_host=https://api.your-cluster.com:6443 \
  --from-literal=cluster_username=kubeadmin \
  --from-literal=cluster_password=your-cluster-password \
  -n mtv-tests
```

Replace the cluster values with your actual OpenShift API endpoint and credentials. This approach keeps sensitive
data out of the Job definition and prevents credential exposure in `oc get job -o yaml` output.

### Step 2: Create and Run Job

Use this template to run tests. Customize the placeholders:

- `[JOB_NAME]` - Unique job name (e.g., `mtv-tier0-tests`, `mtv-warm-tests`, `mtv-copyoffload-tests`)
- `[TEST_MARKERS]` - Pytest marker(s) (e.g., `tier0`, `warm`, `copyoffload`)
- `[TEST_FILTER]` - Optional: specific test name for `-k` flag (omit lines for all tests)

> **Note**: This Job template is also used in `docs/how-to-run-copyoffload-tests.md`. If updating, ensure both
> files remain in sync for the following fields: `apiVersion`, `kind`, `metadata.name`, `spec.template`,
> container `image`, `command`, and `volumeMounts`/`volumes` configuration.

**Template:**

```bash
cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: [JOB_NAME]
  namespace: mtv-tests
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: tests
        image: ghcr.io/redhatqe/mtv-api-tests:latest  # Or use your custom image
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
            uv run pytest -m [TEST_MARKERS] \
              -v \
              ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
              ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
              ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
              --tc=source_provider_type:[PROVIDER_TYPE] \
              --tc=source_provider_version:[PROVIDER_VERSION] \
              --tc=storage_class:[STORAGE_CLASS]
            # Replace [PROVIDER_TYPE] with: vsphere, ovirt, openstack, or ova
            # Replace [PROVIDER_VERSION] with the exact version from your .providers.json key
            # Replace [STORAGE_CLASS] with your OpenShift storage class name
            # Optional: To run a specific test, add: -k [TEST_FILTER]
        volumeMounts:
        - name: config
          mountPath: /app/.providers.json
          subPath: providers.json
      volumes:
      - name: config
        secret:
          secretName: mtv-test-config
EOF
```

### Example: Run tier0 tests

Replace placeholders:

- `[JOB_NAME]` → `mtv-tier0-tests`
- `[TEST_MARKERS]` → `tier0`
- `[PROVIDER_TYPE]` → `vsphere` (or `ovirt`, `openstack`, `ova`)
- `[PROVIDER_VERSION]` → `8.0.3.00400` (must match your `.providers.json` key exactly)
- `[STORAGE_CLASS]` → `rhosqe-ontap-san-block` (your OpenShift storage class)
- Remove the commented `-k` and `[TEST_FILTER]` lines

**Replace cluster configuration:**

- `ghcr.io/redhatqe/mtv-api-tests:latest` - Use this public image, or substitute with your custom image
  from Quick Start Step 1 (e.g., `<YOUR-REGISTRY>/mtv-tests:latest`)
- If you used **Option A** (credentials in Job): Replace the command with explicit `--tc` flags as shown below
- If you used **Option B** (credentials in Secret): The Job template above automatically reads from the Secret
- `vsphere` / `8.0.3.00400` - Your source provider type and version (must match key in `.providers.json`)
- `your-storage-class` - Your OpenShift storage class name

**For Option A (credentials in Job YAML)**, replace the command section with:

```yaml
        command:
          - uv
          - run
          - pytest
          - -m
          - [TEST_MARKERS]
          - -v
          - --tc=cluster_host:https://api.your-cluster.com:6443
          - --tc=cluster_username:kubeadmin
          - --tc=cluster_password:your-cluster-password  # pragma: allowlist secret
          - --tc=source_provider_type:vsphere
          - --tc=source_provider_version:8.0.3.00400
          - --tc=storage_class:your-storage-class
```

⚠️ **Warning**: This exposes credentials in the Job spec visible via `oc get job -o yaml`.
Use Option B with Secret for better security.

### Step 3: Monitor Test Execution

**Follow test logs in real-time**:

```bash
# Replace [JOB_NAME] with your actual job name (e.g., mtv-tier0-tests)
oc logs -n mtv-tests job/[JOB_NAME] -f
```

**Check Job status**:

```bash
oc get jobs -n mtv-tests
# Look for "COMPLETIONS" showing 1/1 = success, 0/1 = still running
```

**Retrieve test results**:

```bash
# Copy JUnit XML report from completed pod (replace [JOB_NAME] with your actual job name)
# Note: pytest writes to /app/junit-report.xml (WORKDIR /app, pytest.ini: --junit-xml=junit-report.xml)
POD_NAME=$(oc get pods -n mtv-tests -l job-name=[JOB_NAME] -o jsonpath='{.items[0].metadata.name}')
oc cp mtv-tests/$POD_NAME:/app/junit-report.xml ./junit-report.xml
```

**Clean up after tests**:

```bash
# Replace [JOB_NAME] with your actual job name
oc delete job [JOB_NAME] -n mtv-tests
```

---

## Copy-Offload: Accelerated Migrations (Advanced)

Copy-offload is an MTV feature that uses the storage backend to directly copy VM disks from vSphere datastores
to OpenShift PVCs using XCOPY operations, bypassing the traditional v2v transfer path. This requires shared storage
infrastructure between vSphere and OpenShift, VAAI (vSphere APIs for Array Integration) enabled on ESXi hosts,
and a configured StorageMap with offload plugin settings.

**For detailed instructions on running copy-offload tests**, including prerequisites, configuration, and
troubleshooting, see:

📖 **[Copy-Offload Testing Guide](docs/how-to-run-copyoffload-tests.md)**

For technical implementation details, see the
[vsphere-xcopy-volume-populator documentation](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator).

---

## Useful Test Options

### Debug and Troubleshooting Flags

Add these flags to your test runs for debugging:

**For containerized runs (Podman/Docker)** - use `uv run pytest`:

```bash
# Enable verbose output
uv run pytest -v                      # Verbose test names

# Enable debug logging
uv run pytest -s -vv                  # Very verbose with output capture disabled

# Set MTV/OpenShift debug level (add as environment variable)
podman run -e OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG ...

# Keep resources after test for inspection
uv run pytest --skip-teardown         # Don't delete VMs, plans, etc. after tests

# Skip data collector (faster, but no resource tracking)
uv run pytest --skip-data-collector   # Don't track created resources

# Change data collector output location
uv run pytest --data-collector-path /tmp/my-logs

# Run a specific test from a marker/suite
uv run pytest -k test_name            # Run only tests matching pattern
uv run pytest -m copyoffload -k test_copyoffload_thin_migration  # Run only thin test
```

**For local developer mode** (after running `uv sync`) - use `uv run pytest` or just `pytest`:

```bash
# Same flags work in local mode
pytest -v                             # Verbose test names
pytest -s -vv                         # Very verbose with output capture disabled
pytest --skip-teardown                # Don't delete VMs, plans, etc. after tests
pytest -k test_name                   # Run only tests matching pattern
```

**Example - Run tier0 with debug mode and keep resources**:

```bash
podman run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -e OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG \
  ghcr.io/redhatqe/mtv-api-tests:latest \
  uv run pytest -s -vv -m tier0 --skip-teardown \
    --tc=cluster_host:https://api.your-cluster.com:6443 \
    --tc=cluster_username:kubeadmin \
    --tc=cluster_password:'your-cluster-password' \  # pragma: allowlist secret
    --tc=source_provider_type:vsphere \
    --tc=storage_class:YOUR-STORAGE-CLASS
```

**When to use these flags**:

- `--skip-teardown` - Test failed and you want to inspect the created VMs/plans
- `--skip-data-collector` - Running many quick tests and don't need resource tracking
- `-s -vv` - Test is failing and you need detailed output to diagnose
- `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG` - Need to see all API calls to OpenShift
- `-k` - Run only specific tests by name pattern (useful for debugging or running individual tests)

> **Note**: All examples in this section show the full command. When using containers, always prefix with
> `podman run ... ghcr.io/redhatqe/mtv-api-tests:latest` before the `uv run pytest` command.

### Running Specific Tests with `-k`

The `-k` flag allows you to run specific tests by matching their names:

```bash
# Run only the thin migration test from copyoffload
podman run ... uv run pytest -k test_copyoffload_thin_migration -v \
  --tc=source_provider_type:vsphere --tc=storage_class:ontap-san-block

# Run multiple tests with pattern matching
podman run ... uv run pytest -k "test_copyoffload_multi_disk" -v ...  # Matches both multi-disk tests
podman run ... uv run pytest -k "thin or thick" -v ...                 # Matches thin and thick tests
```

**List all available test names**:

```bash
# In container
podman run --rm ghcr.io/redhatqe/mtv-api-tests:latest uv run pytest --collect-only -q

# In local developer mode
uv run pytest --collect-only -q
```

---

## Test Results and Reports

Tests automatically generate a **JUnit XML report** (`junit-report.xml`) containing:

- Test results (passed/failed/skipped)
- Execution times
- Error messages and stack traces
- Test metadata

**Accessing the report**:

**From local Podman/Docker run**:

```bash
# Default path: /app/junit-report.xml (WORKDIR /app, pytest.ini: --junit-xml=junit-report.xml)
# Override with --junit-xml to write to a mounted volume for persistence:
podman run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -v $(pwd)/results:/app/results \
  ghcr.io/redhatqe/mtv-api-tests:latest \
  uv run pytest -m tier0 -v \
    --junit-xml=/app/results/junit-report.xml \
    --tc=cluster_host:https://api.your-cluster.com:6443 \
    --tc=cluster_username:kubeadmin \
    --tc=cluster_password:'your-cluster-password' \  # pragma: allowlist secret
    --tc=source_provider_type:vsphere \
    --tc=storage_class:YOUR-STORAGE-CLASS

# Report will be saved to ./results/junit-report.xml
```

**From OpenShift Job**:

```bash
# Copy report from completed pod (default path: /app/junit-report.xml from pytest.ini)
# Replace [JOB_NAME] with your job name, e.g., mtv-tier0-tests
POD_NAME=$(oc get pods -n mtv-tests -l job-name=[JOB_NAME] -o jsonpath='{.items[0].metadata.name}')
oc cp mtv-tests/$POD_NAME:/app/junit-report.xml ./junit-report.xml
```

**View report in CI/CD tools**: Most CI/CD platforms (Jenkins, GitLab CI, GitHub Actions) can parse JUnit XML
for test result dashboards.

---

## Troubleshooting

### Error: "pytest: command not found"

Make sure you're using `uv run pytest` (not just `pytest`):

```bash
# ✅ Correct
podman run ... uv run pytest -m tier0 ...

# ❌ Wrong
podman run ... pytest -m tier0 ...
```

### Authentication Failed

```bash
oc whoami
oc auth can-i create virtualmachines
```

### Provider Connection Failed

```bash
# Test connectivity from cluster
oc run test-curl --rm -it --image=curlimages/curl -- curl -k https://vcenter.example.com

# List available provider keys
cat .providers.json | jq 'keys'

# Verify credentials for a specific provider (replace with your actual key)
cat .providers.json | jq '."vsphere-8.0.1"'
```

### Storage Class Not Found

```bash
oc get storageclass  # Use actual storage class name
```

### Migration Stuck

```bash
# Check MTV operator logs
oc logs -n openshift-mtv deployment/forklift-controller -f

# Check plan status
oc get plans -A
oc describe plan <plan-name> -n openshift-mtv
```

### Collect Debug Information

```bash
oc adm must-gather --image=quay.io/konveyor/forklift-must-gather:latest --dest-dir=/tmp/mtv-logs
```

### Manual Resource Cleanup

If tests fail or you used `--skip-teardown`, clean up manually.

**About the data collector**: The test suite includes a data collector feature that tracks all created
resources in `.data-collector/resources.json`. This file is created automatically during test runs unless
you pass `--skip-data-collector`. When available, use `tools/clean_cluster.py` with the `resources.json`
file for automated cleanup. If the file doesn't exist (e.g., you used `--skip-data-collector`), fall back
to manual `oc delete` commands.

```bash
# Using resource tracker (if data collector was enabled)
uv run tools/clean_cluster.py .data-collector/resources.json

# Or manually delete resources
oc delete vm --all -n <test-namespace>
oc delete plan --all -n openshift-mtv
oc delete provider <provider-name> -n openshift-mtv
```

---

## FAQ

**Q: Do I need Python/pytest/uv on my machine?**
A: No. Everything runs inside the container. You only need Podman or Docker.

**Q: How long do tests take?**
A: Test duration varies. tier0 tests are fastest (smoke tests), warm migration tests include warm migration
scenarios, and copy-offload tests are optimized for speed with shared storage.

**Q: Can I run on SNO (Single Node OpenShift)?**
A: Yes. SNO has been validated with copy-offload tests. Other test types may work but have not
been specifically validated on SNO.

**Q: Do tests generate reports?**
A: Yes. Tests automatically generate a JUnit XML report (`junit-report.xml`) with test results, execution times,
and error details. See the "Test Results and Reports" section for how to access it.

**Q: How do I debug a failing test?**
A: Use `--skip-teardown` to keep resources after test, and `-s -vv` for verbose output.
Set `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG` for API call logs. See the "Useful Test Options" section for details.

---

## Advanced Topics

### Running Locally Without Container

**For test developers** who want to run tests directly on their machine (requires manual setup).

### Prerequisites (Must Install Manually)

**System packages**:

> **Note**: uv automatically downloads and manages Python versions—no system Python installation needed. However,
> the packages below are system-level compilation dependencies required by Python extensions used by the test suite

```bash
# RHEL/Fedora
sudo dnf install gcc clang libxml2-devel libcurl-devel openssl-devel

# Ubuntu/Debian
sudo apt install gcc clang libxml2-dev libcurl4-openssl-dev libssl-dev

# macOS
brew install gcc libxml2 curl openssl
```

**Required tools**:

- uv package manager (manages Python automatically)
- oc CLI
- virtctl

> **Note**: uv will automatically download and manage the appropriate Python version. However, if you encounter
> HTTPS-related issues with Python 3.13+, consider using Python 3.12 which has been tested and verified to work.

### Setup and Run

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone repository and install dependencies
git clone https://github.com/RedHatQE/mtv-api-tests.git
cd mtv-api-tests
uv sync  # uv will automatically handle Python version

# 3. Run tests
uv run pytest -v \
  --tc=cluster_host:https://api.cluster.com:6443 \
  --tc=cluster_username:kubeadmin \
  --tc=cluster_password:'PASSWORD' \  # pragma: allowlist secret
  --tc=source_provider_type:vsphere \
  --tc=source_provider_version:8.0.1 \
  --tc=storage_class:standard-csi

# For debug options (--skip-teardown, -s -vv, etc.), see "Useful Test Options" section above
```
