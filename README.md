# MTV API Test Suite

Test suite for validating VM migrations to OpenShift from VMware vSphere,
RHV, and OpenStack using Migration Toolkit for Virtualization (MTV).

---

## Prerequisites

### Local Machine Requirements

- **OpenShift cluster** with MTV operator installed
- **oc CLI** - Download from your cluster console
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
oc whoami                                # Check cluster access
oc get csv -n openshift-mtv | grep mtv  # Verify MTV operator
docker --version                         # or: podman --version
```

---

## Quick Start

### 1. Grant Permissions

Give your OpenShift user permissions to create MTV resources and VMs:

```bash
oc adm policy add-cluster-role-to-user cluster-admin $(oc whoami)
```

### 2. Configure Your Source Provider

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
  "vsphere": {
    "version": "8.0.1",
    "hostname": "vcenter.example.com",
    "username": "administrator@vsphere.local",
    "password": "your-password",  <!-- pragma: allowlist secret -->
    "datacenter": "DC1",
    "cluster": "Cluster1",
    "vm_base_name": "rhel8-template"
  }
}
```

**Field descriptions**:

- `version` - Your vSphere version
- `hostname` - vCenter hostname or IP address
- `username` - vCenter admin username
- `password` - vCenter password
- `datacenter` - Your datacenter name
- `cluster` - Your cluster name
- `vm_base_name` - Base VM to clone for tests (must exist and be powered off)

**All fields are required.**

---

**RHV Example:**

<!-- pragma: allowlist secret -->
```json
{
  "rhv": {
    "version": "4.4",
    "hostname": "rhvm.example.com",
    "username": "admin@internal",
    "password": "your-password", <!-- pragma: allowlist secret -->
    "datacenter": "Default",
    "cluster": "Default",
    "template_name": "rhel8-template",
    "ca_cert": ""
  }
}
```

**Field descriptions**:

- `version` - RHV version
- `hostname` - RHV Manager hostname
- `username` - RHV admin username
- `password` - RHV password
- `datacenter` - Your datacenter name
- `cluster` - Your cluster name
- `template_name` - Template to use for tests (must exist)
- `ca_cert` - Optional: Path to CA cert file (leave empty to skip verification)

**Required fields**: All except `ca_cert` (optional).

---

**OpenStack Example:**

<!-- pragma: allowlist secret -->
```json
{
  "openstack": {
    "version": "17.1",
    "auth_url": "https://openstack.example.com:5000/v3",
    "username": "admin",
    "password": "your-password", <!-- pragma: allowlist secret -->
    "project_name": "admin",
    "domain_name": "Default",
    "region": "RegionOne",
    "base_vm_name": "rhel8-template"
  }
}
```

**Field descriptions**:

- `version` - OpenStack version
- `auth_url` - Keystone authentication URL
- `username` - OpenStack username
- `password` - OpenStack password
- `project_name` - Project/tenant name
- `domain_name` - Domain name
- `region` - Region name
- `base_vm_name` - Base instance to clone for tests (must exist)

**All fields are required.**

---

### 3. Find Your Storage Class

Check which storage classes are available in your OpenShift cluster:

```bash
oc get storageclass
```

Pick one that supports block storage (e.g., `ocs-storagecluster-ceph-rbd`, `ontap-san-block`).
You'll use this name in the next step.

### 4. Run Your First Test

Execute tier1 tests (smoke tests) using the containerized test suite:

**Using Docker**:

```bash
docker run --rm \
  -v ~/.kube/config:/root/.kube/config:ro \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -m tier1 -v \
    --tc=source_provider_type:vsphere \
    --tc=source_provider_version:8.0.1 \
    --tc=storage_class:YOUR-STORAGE-CLASS
```

**Using Podman** (RHEL/Fedora with SELinux):

```bash
podman run --rm \
  -v ~/.kube/config:/root/.kube/config:ro,z \
  -v $(pwd)/.providers.json:/app/.providers.json:ro,z \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -m tier1 -v \
    --tc=source_provider_type:vsphere \
    --tc=source_provider_version:8.0.1 \
    --tc=storage_class:YOUR-STORAGE-CLASS
```

**Replace**:

- `YOUR-STORAGE-CLASS` → Your storage class from step 3
- `vsphere` → `rhv` or `openstack` if using different provider
- `8.0.1` → Your provider version

---

## Running Different Test Categories

The Quick Start runs **tier1** tests (smoke tests). You can run other test categories by changing the `-m` marker:

| Marker | What It Tests | When to Use |
|--------|---------------|-------------|
| `tier1` | Smoke tests - critical paths | First run, quick validation |
| `tier2` | Full functional test suite | Complete testing |
| `copyoffload` | Fast migrations via shared storage | Testing storage arrays |
| `warm` | Warm migrations (VMs stay running) | Specific scenario testing |
| `cold` | Cold migrations (VMs powered off) | Specific scenario testing |

**Examples** - Change `-m tier1` to run different tests:

```bash
# Tier2 tests
docker run ... uv run pytest -m tier2 -v --tc=source_provider_type:vsphere ...

# Copy-offload tests
docker run ... uv run pytest -m copyoffload -v --tc=source_provider_type:vsphere ...

# Combine markers
docker run ... uv run pytest -m "tier1 or tier2" -v --tc=source_provider_type:vsphere ...
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
  "vsphere": {
    "version": "8.0.3.00400",
    "hostname": "vcenter.example.com",
    "username": "administrator@vsphere.local",
    "password": "your-password", <!-- pragma: allowlist secret -->
    "datacenter": "DC1",
    "cluster": "Cluster1",
    "vm_base_name": "rhel8-template",
    "copyoffload": {
      "storage_vendor_product": "ontap",
      "datastore_id": "datastore-123",
      "template_name": "rhel9-template",
      "storage_hostname": "storage.example.com",
      "storage_username": "admin",
      "storage_password": "password",  <!-- pragma: allowlist secret -->
      "ontap_svm": "vserver-name",
      "esxi_host": "esxi01.example.com"
    }
  }
}
```

**Vendor-specific fields**:

- NetApp ONTAP: `ontap_svm`
- Pure Storage: `pure_cluster_prefix`
- PowerMax: `powermax_symmetrix_id`
- PowerFlex: `powerflex_system_id`

**Run copy-offload tests**:

```bash
docker run --rm -v ~/.kube/config:/root/.kube/config:ro \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -m copyoffload -v \
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

**Example - Run tier1 with debug mode and keep resources**:

```bash
docker run --rm \
  -v ~/.kube/config:/root/.kube/config:ro \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -e OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -s -vv -m tier1 --skip-teardown \
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
| **Best for** | Quick tier1 tests, development | Tier2 tests, overnight runs, CI/CD |
| **Runs where** | Your local machine | Inside OpenShift cluster |
| **Config source** | Local `.providers.json` file | OpenShift secret |
| **Can disconnect?** | ❌ No - must stay connected | ✅ Yes - tests continue |
| **Setup** | Simple - just run docker command | Requires creating secret + Job |

**How OpenShift Jobs work**:

1. **Create secret**: Store your `.providers.json` in OpenShift (one-time setup)
2. **Create Job**: Define what tests to run (tier1, tier2, copyoffload, etc.)
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

### Example 1: Run tier1 tests (smoke tests)

```bash
cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: mtv-tier1-tests
  namespace: mtv-tests
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: tests
        image: quay.io/openshift-cnv/mtv-tests:latest
        command:
          - uv
          - run
          - pytest
          - -m
          - tier1
          - -v
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
        image: quay.io/openshift-cnv/mtv-tests:latest
        command:
          - uv
          - run
          - pytest
          - -m
          - copyoffload
          - -v
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
oc logs -n mtv-tests job/mtv-tier1-tests -f
```

**Check if Job completed successfully**:

```bash
oc get jobs -n mtv-tests
# Look for "COMPLETIONS" showing 1/1 = success
```

**Clean up after tests finish**:

```bash
oc delete job mtv-tier1-tests -n mtv-tests
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
  -v ~/.kube/config:/root/.kube/config:ro \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -v $(pwd)/results:/app \
  quay.io/openshift-cnv/mtv-tests:latest \
  uv run pytest -m tier1 -v \
    --tc=source_provider_type:vsphere \
    --tc=storage_class:YOUR-STORAGE-CLASS

# Report will be saved to ./results/junit-report.xml
```

**From OpenShift Job**:

```bash
# Copy report from completed pod
POD_NAME=$(oc get pods -n mtv-tests -l job-name=mtv-tier1-tests -o jsonpath='{.items[0].metadata.name}')
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
docker run ... uv run pytest -m tier1 ...

# ❌ Wrong
docker run ... pytest -m tier1 ...
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
A: No. Everything runs inside the container. You only need Docker/Podman and `oc` CLI.

**Q: How long do tests take?**  
A: Test duration varies. Tier1 tests are fastest (smoke tests), tier2 runs the full suite, and copy-offload
tests are optimized for speed with shared storage.

**Q: Can I run on SNO (Single Node OpenShift)?**  
A: Yes. Tests work on SNO clusters.

**Q: What's the difference between Docker run and OpenShift Job?**  
A: Docker run uses local `.providers.json`. Job uses OpenShift secret and runs inside the cluster.

**Q: Where do I get cloud-init script for copy-offload?**  
A: Contact the team if you need the cloud-init configuration for copy-offload testing.

**Q: Do tests generate reports?**  
A: Yes. Tests automatically generate a JUnit XML report (`junit-report.xml`) with test results, execution times,
and error details. See the "Test Results and Reports" section for how to access it.

**Q: How do I debug a failing test?**  
A: Use `--skip-teardown` to keep resources after test, and `-s -vv` for verbose output.
Set `OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG` for API call logs. See the "Useful Test Options" section for details.

---

## Advanced Topics

### Building the Container Image Locally

If you need to build the test image yourself (for development or customization):

```bash
# Clone the repository
git clone https://github.com/your-org/mtv-api-tests.git
cd mtv-api-tests

# Build with Docker
docker build -t mtv-api-tests:latest .

# Or build with Podman
podman build -t mtv-api-tests:latest .

# Run your locally built image
docker run --rm \
  -v ~/.kube/config:/root/.kube/config:ro \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  mtv-api-tests:latest \
  uv run pytest -m tier1 -v \
    --tc=source_provider_type:vsphere \
    --tc=storage_class:YOUR-STORAGE-CLASS
```

**Optional: Push to your own registry**:

```bash
docker tag mtv-api-tests:latest quay.io/your-org/mtv-tests:latest
docker push quay.io/your-org/mtv-tests:latest
```

---

### Running Locally Without Container

**For test developers** who want to run tests directly on their machine (requires manual setup).

### Prerequisites (Must Install Manually)

**System packages**:

```bash
# RHEL/Fedora
sudo dnf install gcc clang python3-devel libxml2-devel libcurl-devel openssl-devel

# Ubuntu/Debian
sudo apt install gcc clang python3-dev libxml2-dev libcurl4-openssl-dev libssl-dev

# macOS
brew install gcc python@3.12 libxml2 curl openssl
```

**Required tools**:

- Python 3.12+
- uv package manager
- oc CLI
- virtctl

### Setup and Run

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone repository and install dependencies
git clone https://github.com/your-org/mtv-api-tests.git  # Replace with actual repo URL
cd mtv-api-tests
uv sync

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
