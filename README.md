# MTV API Test Suite

Test suite for validating VM migrations to OpenShift from VMware vSphere,
RHV, and OpenStack using Migration Toolkit for Virtualization (MTV).

---

## Prerequisites

### Local Machine Requirements

- **OpenShift cluster** with MTV operator installed
- **Docker or Podman** - To run the test container

### Source Provider Requirements

You need a base VM/template in your source provider:

| Provider | Resource Type | Requirements |
|----------|--------------|--------------|
| **VMware vSphere** | VM | Powered off, QEMU guest agent installed |
| **RHV/oVirt** | Template | Min 1536 MiB memory |
| **OpenStack** | Instance | ACTIVE/SHUTOFF state, QEMU guest agent installed |

**For copy-offload tests only**: VM must have cloud-init script configured (available on request).

### Verify Setup

```bash
docker --version  # or: podman --version
```

**Optional** - If you have `oc` CLI installed, you can verify your cluster:

```bash
oc whoami                                # Check cluster access
oc get csv -n openshift-mtv | grep mtv  # Verify MTV operator
```

---

## Quick Start

### 1. Build and Push the Test Image

**Important**: The test image is not available in a public registry. You must build and push it to your own registry first.

> **TBD**: A pre-built public image will be provided in the future. Once available, you can skip this step and use
> the public image directly.

```bash
# Clone the repository
git clone <repository-url>
cd mtv-api-tests

# Build the image (use 'podman' instead of 'docker' if using Podman)
docker build -t <YOUR-REGISTRY>/mtv-tests:latest .

# Push to your registry
docker push <YOUR-REGISTRY>/mtv-tests:latest
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

- Connect to your source provider (vSphere, RHV, or OpenStack)
- Find the base VM to clone for testing
- Create test VMs and perform migrations

**What should it include?**

- Connection details (hostname, credentials)
- Location information (datacenter, cluster)
- Base VM/template name to use for testing

Create a `.providers.json` file in your current directory with your provider's details:

**VMware vSphere Example:**

<!-- pragma: allowlist secret -->
```json
{
  "vsphere-8.0.1": {
    "type": "vsphere",
    "version": "8.0.1",
    "fqdn": "vcenter.example.com",
    "api_url": "https://vcenter.example.com/sdk",
    "username": "administrator@vsphere.local",
    "password": "your-password",  <!-- pragma: allowlist secret -->
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password"  <!-- pragma: allowlist secret -->
  }
}
```

**Field descriptions**:

- **Key format**: `"vsphere-8.0.1"` - Must be `{type}-{version}`
- `type` - Provider type (always `"vsphere"`)
- `version` - Your vSphere version (must match the key)
- `fqdn` - vCenter hostname or IP address
- `api_url` - vCenter API endpoint (format: `https://{fqdn}/sdk`)
- `username` - vCenter admin username
- `password` - vCenter password
- `guest_vm_linux_user` - Username for SSH to Linux VMs (usually `root`)
- `guest_vm_linux_password` - Password for Linux VMs

**All fields are required.**

---

**RHV Example:**

<!-- pragma: allowlist secret -->
```json
{
  "ovirt-4.4": {
    "type": "ovirt",
    "version": "4.4",
    "fqdn": "rhvm.example.com",
    "api_url": "https://rhvm.example.com/ovirt-engine/api",
    "username": "admin@internal",
    "password": "your-password"  <!-- pragma: allowlist secret -->
  }
}
```

**Field descriptions**:

- **Key format**: `"ovirt-4.4"` - Must be `{type}-{version}`
- `type` - Provider type (always `"ovirt"`)
- `version` - RHV version (must match the key)
- `fqdn` - RHV Manager hostname or IP address
- `api_url` - RHV API endpoint (format: `https://{fqdn}/ovirt-engine/api`)
- `username` - RHV admin username
- `password` - RHV password

**All fields are required.**

---

**OpenStack Example:**

<!-- pragma: allowlist secret -->
```json
{
  "openstack-17.1": {
    "type": "openstack",
    "version": "17.1",
    "fqdn": "openstack.example.com",
    "api_url": "https://openstack.example.com:5000/v3",
    "username": "admin",
    "password": "your-password",  <!-- pragma: allowlist secret -->
    "user_domain_name": "Default",
    "region_name": "RegionOne",
    "project_name": "admin",
    "user_domain_id": "default",
    "project_domain_id": "default",
    "guest_vm_linux_user": "cloud-user",
    "guest_vm_linux_password": "your-vm-password"  <!-- pragma: allowlist secret -->
  }
}
```

**Field descriptions**:

- **Key format**: `"openstack-17.1"` - Must be `{type}-{version}`
- `type` - Provider type (always `"openstack"`)
- `version` - OpenStack version (must match the key)
- `fqdn` - OpenStack hostname or IP address
- `api_url` - Keystone authentication URL (typically port 5000)
- `username` - OpenStack username
- `password` - OpenStack password
- `user_domain_name` - User domain name
- `region_name` - Region name
- `project_name` - Project/tenant name
- `user_domain_id` - User domain ID
- `project_domain_id` - Project domain ID
- `guest_vm_linux_user` - Username for SSH to Linux VMs
- `guest_vm_linux_password` - Password for Linux VMs

**All fields are required.**

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
docker run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  <YOUR-REGISTRY>/mtv-tests:latest \
  uv run pytest -m tier0 -v \
    --tc=cluster_host:https://api.your-cluster.com:6443 \
    --tc=cluster_username:kubeadmin \
    --tc=cluster_password:'your-cluster-password' \  # pragma: allowlist secret
    --tc=source_provider_type:vsphere \
    --tc=source_provider_version:8.0.1 \
    --tc=storage_class:YOUR-STORAGE-CLASS
```

> **Note**: Replace `docker` with `podman` if using Podman. On RHEL/Fedora with SELinux, add `,z` to volume mounts: `-v $(pwd)/.providers.json:/app/.providers.json:ro,z`

**Replace**:

- `<YOUR-REGISTRY>` → Your container registry from step 1 (e.g., `quay.io/youruser`)
- `https://api.your-cluster.com:6443` → Your OpenShift API URL
- `kubeadmin` → Your cluster username
- `your-cluster-password` → Your cluster password
- `YOUR-STORAGE-CLASS` → Your storage class from step 4
- `vsphere` → Provider type from your `.providers.json` key: `vsphere`, `ovirt`, or `openstack`
- `8.0.1` → Provider version from your `.providers.json` key (must match exactly)

---

## Running Different Test Categories

The Quick Start runs **tier0** tests (smoke tests). You can run other test categories by changing the `-m` marker:

| Marker | What It Tests | When to Use |
|--------|---------------|-------------|
| `tier0` | Smoke tests - critical paths | First run, quick validation |
| `copyoffload` | Fast migrations via shared storage | Testing storage arrays |
| `warm` | Warm migrations (VMs stay running) | Specific scenario testing |

**Examples** - Change `-m tier0` to run different tests:

```bash
# Warm migration tests
docker run ... uv run pytest -m warm -v --tc=source_provider_type:vsphere ...

# Copy-offload tests
docker run ... uv run pytest -m copyoffload -v --tc=source_provider_type:vsphere ...

# Combine markers
docker run ... uv run pytest -m "tier0 or warm" -v --tc=source_provider_type:vsphere ...
```

---

## Copy-Offload: Accelerated Migrations (Advanced)

**What is copy-offload?** A feature that speeds up migrations 3-5x when your source and target use shared
storage arrays.

**When to use this**: If your vSphere datastore and OpenShift storage use the same storage backend
(NetApp ONTAP, Pure Storage, Dell PowerMax/PowerFlex).

**Requirements**:

- Shared storage array between source and target
- Source VM must have cloud-init configured (contact team for cloud-init script)

**Setup**: Add `copyoffload` section to your `.providers.json`:

<!-- pragma: allowlist secret -->
```json
{
  "vsphere-8.0.3.00400": {
    "type": "vsphere",
    "version": "8.0.3.00400",
    "fqdn": "vcenter.example.com",
    "api_url": "https://vcenter.example.com/sdk",
    "username": "administrator@vsphere.local",
    "password": "your-password",  <!-- pragma: allowlist secret -->
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password",  <!-- pragma: allowlist secret -->
    "copyoffload": {
      "storage_vendor_product": "ontap",
      "datastore_id": "datastore-123",
      "default_vm_name": "rhel9-template",
      "storage_hostname": "storage.example.com",
      "storage_username": "admin",
      "storage_password": "your-storage-password",  <!-- pragma: allowlist secret -->
      "ontap_svm": "vserver-name",
      "esxi_clone_method": "ssh",
      "esxi_host": "esxi01.example.com",
      "esxi_user": "root",
      "esxi_password": "your-esxi-password"  <!-- pragma: allowlist secret -->
    }
  }
}
```

**Copy-offload required fields**:

- `storage_vendor_product` - Storage vendor (`"ontap"` or `"vantara"`)
- `datastore_id` - vSphere datastore ID (e.g., `"datastore-123"`)
- `default_vm_name` - Template/VM name to use for copy-offload tests
- `storage_hostname` - Storage array hostname/IP
- `storage_username` - Storage array username
- `storage_password` - Storage array password

**Optional fields**:

- `esxi_clone_method` - Clone method (`"vib"` default, or `"ssh"`)
- `esxi_host` - ESXi hostname (required if using `"ssh"` method)
- `esxi_user` - ESXi username (required if using `"ssh"` method)
- `esxi_password` - ESXi password (required if using `"ssh"` method)

**Vendor-specific fields**:

- NetApp ONTAP: `ontap_svm` - SVM/vServer name
- Pure Storage: `pure_cluster_prefix` - Cluster prefix
- PowerMax: `powermax_symmetrix_id` - Symmetrix ID
- PowerFlex: `powerflex_system_id` - System ID

**Run copy-offload tests**:

```bash
docker run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  <YOUR-REGISTRY>/mtv-tests:latest \
  uv run pytest -m copyoffload -v \
    --tc=cluster_host:https://api.your-cluster.com:6443 \
    --tc=cluster_username:kubeadmin \
    --tc=cluster_password:'your-cluster-password' \  # pragma: allowlist secret
    --tc=source_provider_type:vsphere \
    --tc=source_provider_version:8.0.3.00400 \
    --tc=storage_class:ontap-san-block
```

---

## Useful Test Options

### Debug and Troubleshooting Flags

Add these flags to any test run (Docker, Podman, or local) for debugging:

```bash
# Enable verbose output
pytest -v                      # Verbose test names

# Enable debug logging
pytest -s -vv                  # Very verbose with output capture disabled

# Set MTV/OpenShift debug level
export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG
docker run -e OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG ...

# Keep resources after test for inspection
pytest --skip-teardown         # Don't delete VMs, plans, etc. after tests

# Skip data collector (faster, but no resource tracking)
pytest --skip-data-collector   # Don't track created resources

# Change data collector output location
pytest --data-collector-path /tmp/my-logs

# Run a specific test from a marker/suite
pytest -k test_name  # Run only tests matching pattern
pytest -m copyoffload -k test_copyoffload_thin_migration  # Run only thin test from copyoffload marker
```

**Example - Run tier0 with debug mode and keep resources**:

```bash
docker run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -e OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG \
  <YOUR-REGISTRY>/mtv-tests:latest \
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

### Running Specific Tests with `-k`

The `-k` flag allows you to run specific tests by matching their names:

```bash
# Run only the thin migration test from copyoffload
docker run ... uv run pytest -k test_copyoffload_thin_migration -v \
  --tc=source_provider_type:vsphere --tc=storage_class:ontap-san-block

# Run multiple tests with pattern matching
docker run ... uv run pytest -k "test_copyoffload_multi_disk" -v ...  # Matches both multi-disk tests
docker run ... uv run pytest -k "thin or thick" -v ...                 # Matches thin and thick tests
```

Use `pytest --collect-only -q` to list all available test names in the suite.

---

## Running as OpenShift Job

The Quick Start runs tests from your local machine. For **long-running or automated tests**,
run them as OpenShift Jobs instead.

| | Local Docker/Podman (Quick Start) | OpenShift Job |
|---|---|---|
| **Best for** | Quick tier0 tests, development | Long-running tests, overnight runs, CI/CD |
| **Runs where** | Your local machine | Inside OpenShift cluster |
| **Config source** | Local `.providers.json` file | OpenShift secret |
| **Can disconnect?** | ❌ No - must stay connected | ✅ Yes - tests continue |
| **Setup** | Simple - just run container | Requires creating secret + Job |

**How OpenShift Jobs work**:

1. **Create secret**: Store your `.providers.json` in OpenShift (one-time setup)
2. **Create Job**: Define what tests to run (tier0, warm, copyoffload, etc.)
3. **Job executes**: OpenShift schedules and runs the test container
4. **View logs**: Use `oc logs` to see results

### 1. Store Configuration as Secret

This stores your provider credentials securely in OpenShift:

```bash
oc create namespace mtv-tests
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  -n mtv-tests
```

### 2. Create and Run Job

Choose which tests to run. Each example below creates a Job that:

- Pulls the test container image
- Loads your provider config from the secret
- Executes the specified tests
- Writes results to pod logs

### Example 1: Run tier0 tests (smoke tests)

```bash
cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: mtv-tier0-tests
  namespace: mtv-tests
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: tests
        image: <YOUR-REGISTRY>/mtv-tests:latest
        command:
          - uv
          - run
          - pytest
          - -m
          - tier0
          - -v
          - --tc=cluster_host:https://api.your-cluster.com:6443
          - --tc=cluster_username:kubeadmin
          - --tc=cluster_password:your-cluster-password  # pragma: allowlist secret
          - --tc=source_provider_type:vsphere
          - --tc=source_provider_version:8.0.1
          - --tc=storage_class:YOUR-STORAGE-CLASS
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

### Example 2: Run copy-offload tests (fast migrations)

```bash
cat <<EOF | oc apply -f -
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
        image: <YOUR-REGISTRY>/mtv-tests:latest
        command:
          - uv
          - run
          - pytest
          - -m
          - copyoffload
          - -v
          - --tc=cluster_host:https://api.your-cluster.com:6443
          - --tc=cluster_username:kubeadmin
          - --tc=cluster_password:your-cluster-password  # pragma: allowlist secret
          - --tc=source_provider_type:vsphere
          - --tc=source_provider_version:8.0.3.00400
          - --tc=storage_class:ontap-san-block
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

### 3. Monitor and Manage Jobs

**View test execution logs** (follow in real-time):

```bash
oc logs -n mtv-tests job/mtv-tier0-tests -f
```

**Check if Job completed successfully**:

```bash
oc get jobs -n mtv-tests
# Look for "COMPLETIONS" showing 1/1 = success
```

**Clean up after tests finish**:

```bash
oc delete job mtv-tier0-tests -n mtv-tests
```

---

## Test Results and Reports

Tests automatically generate a **JUnit XML report** (`junit-report.xml`) containing:

- Test results (passed/failed/skipped)
- Execution times
- Error messages and stack traces
- Test metadata

**Accessing the report**:

**From local Docker/Podman run**:

```bash
# Mount a volume to save the report
docker run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -v $(pwd)/results:/app \
  <YOUR-REGISTRY>/mtv-tests:latest \
  uv run pytest -m tier0 -v \
    --tc=cluster_host:https://api.your-cluster.com:6443 \
    --tc=cluster_username:kubeadmin \
    --tc=cluster_password:'your-cluster-password' \  # pragma: allowlist secret
    --tc=source_provider_type:vsphere \
    --tc=storage_class:YOUR-STORAGE-CLASS

# Report will be saved to ./results/junit-report.xml
```

**From OpenShift Job**:

```bash
# Copy report from completed pod
POD_NAME=$(oc get pods -n mtv-tests -l job-name=mtv-tier0-tests -o jsonpath='{.items[0].metadata.name}')
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
docker run ... uv run pytest -m tier0 ...

# ❌ Wrong
docker run ... pytest -m tier0 ...
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

# Verify credentials
cat .providers.json | jq '.vsphere'
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

If tests fail or you used `--skip-teardown`, clean up manually:

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
A: No. Everything runs inside the container. You only need Docker or Podman.

**Q: How long do tests take?**
A: Test duration varies. Tier0 tests are fastest (smoke tests), warm migration tests include warm migration
scenarios, and copy-offload tests are optimized for speed with shared storage.

**Q: Can I run on SNO (Single Node OpenShift)?**
A: Yes. Tests work on SNO clusters.

**Q: What's the difference between Docker run and OpenShift Job?**
A: Docker run uses local `.providers.json`. Job uses OpenShift secret and runs inside the cluster.

**Q: Where do I get cloud-init script for copy-offload?**
A: Contact the copyoffload development or QE teams if you need the cloud-init configuration for copy-offload
testing. You can also reach out through your Red Hat support channels or open an issue in the project repository.

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

> **Note**: Python is not listed as a requirement because `uv` automatically manages Python versions. The packages
> below are compilation dependencies for Python extensions.

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
git clone https://github.com/your-org/mtv-api-tests.git  # Replace with actual repo URL
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

> **Note**: The containerized approach (Quick Start) is **strongly recommended**. Local setup requires manual
> installation of system dependencies and is primarily for test development.
