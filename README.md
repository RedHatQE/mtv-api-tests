# MTV API Test Suite

Test suite for validating VM migrations to OpenShift from VMware vSphere,
RHV, and OpenStack using Migration Toolkit for Virtualization (MTV).

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
|----------|--------------|--------------|
| **VMware vSphere** | VM | Powered off, QEMU guest agent installed |
| **RHV/oVirt** | Template | Min 1536 MiB memory |
| **OpenStack** | Instance | ACTIVE/SHUTOFF state, QEMU guest agent installed |
| **OVA** ⚠️ Tech Preview | OVA file | NFS-accessible OVA files |

**For copy-offload tests only**: VM must have cloud-init script configured (available on request).

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

```bash
# Use the pre-built image directly
IMAGE=ghcr.io/redhatqe/mtv-api-tests:latest
```

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

- Connect to your source provider (vSphere, RHV, or OpenStack)
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

Create a `.providers.json` file in your current directory with your provider's details:

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

```json
{
  "ovirt-4.4": {
    "type": "ovirt",
    "version": "4.4",
    "fqdn": "rhvm.example.com",
    "api_url": "https://rhvm.example.com/ovirt-engine/api",
    "username": "admin@internal",
    "password": "your-password",  # pragma: allowlist secret
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password"  # pragma: allowlist secret
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
- `guest_vm_linux_user` - Username for SSH to Linux VMs (usually `root`)
- `guest_vm_linux_password` - Password for Linux VMs

**All fields are required.**

---

**OVA Example:** ⚠️ **Technology Preview**

> **Note**: OVA provider is in Technology Preview and not supported for production use.

```json
{
  "ova-1.0": {
    "type": "ova",
    "version": "1.0",
    "fqdn": "nfs-server.example.com",
    "api_url": "nfs://nfs-server.example.com/path/to/ova-files",
    "username": "nfs-user",
    "password": "your-password",  # pragma: allowlist secret
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password"  # pragma: allowlist secret
  }
}
```

**Field descriptions**:

- **Key format**: `"ova-1.0"` - Must be `{type}-{version}`
- `type` - Provider type (always `"ova"`)
- `version` - Version placeholder (can be any value, e.g., "1.0")
- `fqdn` - NFS server hostname or IP address
- `api_url` - NFS share URL where OVA files are located (format: `nfs://{hostname}/path`)
- `username` - NFS username (if authentication required)
- `password` - NFS password (if authentication required)
- `guest_vm_linux_user` - Username for SSH to Linux VMs (usually `root`)
- `guest_vm_linux_password` - Password for Linux VMs

**All fields are required.**

---

**OpenStack Example:**

```json
{
  "openstack-17.1": {
    "type": "openstack",
    "version": "17.1",
    "fqdn": "openstack.example.com",
    "api_url": "https://openstack.example.com:5000/v3",
    "username": "admin",
    "password": "your-password",  # pragma: allowlist secret
    "user_domain_name": "Default",
    "region_name": "RegionOne",
    "project_name": "admin",
    "user_domain_id": "default",
    "project_domain_id": "default",
    "guest_vm_linux_user": "cloud-user",
    "guest_vm_linux_password": "your-vm-password"  # pragma: allowlist secret
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
podman run ... uv run pytest -m warm -v --tc=source_provider_type:vsphere ...

# Copy-offload tests
podman run ... uv run pytest -m copyoffload -v --tc=source_provider_type:vsphere ...

# Combine markers
podman run ... uv run pytest -m "tier0 or warm" -v --tc=source_provider_type:vsphere ...
```

---

## Copy-Offload: Accelerated Migrations (Advanced)

**What is copy-offload?** Copy-offload uses the vsphere-xcopy-volume-populator to leverage
array-based cloning for accelerated VM migrations from vSphere to OpenShift when both environments
share compatible storage infrastructure.

For technical details, see the [vsphere-xcopy-volume-populator documentation](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator).

---

### Copy-Offload Prerequisites

Before running copy-offload tests, ensure your environment meets these requirements:

#### 1. **VMware Environment**

- **ESXi + vCenter** (recommended) or standalone ESXi
- **Clone method configured**: Choose either VIB or SSH method
  - **VIB**: Requires pre-installing VMware Installation Bundle on ESXi hosts
  - **SSH**: Requires SSH access to ESXi hosts (simpler setup)
  - See setup guide: [Clone Methods (VIB vs SSH)](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#clone-methods-vib-vs-ssh)

#### 2. **Shared Storage Configuration**

- **Tested storage vendors**: These tests currently support:
  - ✅ **NetApp ONTAP** (fully implemented)
  - ✅ **Hitachi Vantara** (fully implemented)
- **Additional vendors supported by copy-offload feature** (not yet implemented in test suite):
  - Pure Storage, Dell (PowerMax/PowerFlex/PowerStore), HPE Primera/3PAR, Infinidat, IBM FlashSystem
  - Full vendor list: [Supported Storage Providers](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#supported-storage-providers)
- **Storage type**: Must be SAN/Block (iSCSI or FC) - **NFS is not supported** for xcopy
- **Configuration**: Same physical storage accessible from both VMware and OpenShift
  - Use matching configurations (e.g., same NetApp SVM for both environments)

#### 3. **OpenShift Environment**

- **CNV (OpenShift Virtualization)** installed
- **Storage configured**:
  - Block storage class with vendor CSI driver (iSCSI/FC) - same storage as VMware
  - File-based storage class for CDI scratch space (if needed)
  - VolumeSnapshot classes configured per vendor CSI guide
- **MTV (Migration Toolkit for Virtualization)** installed:
  - For versions before 2.11: Enable copy-offload by adding to ForkliftController spec:

    ```yaml
    spec:
      feature_copy_offload: 'true'
    ```

  - Configure storage secret in `openshift-mtv` namespace (see Configuration section below)
  - Configure StorageMap for copy-offload storage mapping

#### 4. **Template VM with Cloud-Init**

Create or use an existing VM in vSphere configured with cloud-init for testing. The VM must have:

- SSH access enabled with root user
- Serial console configured for post-migration verification
- Network configuration for connectivity

**Cloud-init script**: The cloud-init configuration for copy-offload testing is maintained separately.
To request access to the recommended cloud-init script and VM preparation guide, please:

- Open an issue in this repository with the label "cloud-init-access"
- Contact the MTV QE team through your Red Hat support channels
- Reach out to the development team in the project communication channels

Once you have the cloud-init script configured, use this VM name as the `default_vm_name` in your
copy-offload configuration.

---

### Configuration

Add the `copyoffload` section to your `.providers.json` file:

```json
{
  "vsphere-8.0.3.00400": {
    "type": "vsphere",
    "version": "8.0.3.00400",
    "fqdn": "vcenter.example.com",
    "api_url": "https://vcenter.example.com/sdk",
    "username": "administrator@vsphere.local",
    "password": "your-password",  # pragma: allowlist secret
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password",  # pragma: allowlist secret
    "copyoffload": {
      "storage_vendor_product": "ontap",
      "datastore_id": "datastore-123",
      "default_vm_name": "rhel9-cloud-init-template",
      "storage_hostname": "storage.example.com",
      "storage_username": "admin",
      "storage_password": "your-storage-password",  # pragma: allowlist secret
      "ontap_svm": "vserver-name",
      "esxi_clone_method": "ssh",
      "esxi_host": "esxi01.example.com",
      "esxi_user": "root",
      "esxi_password": "your-esxi-password"  # pragma: allowlist secret
    }
  }
}
```

#### Copy-offload Required Fields

- `storage_vendor_product` - Storage vendor product name (currently supported: `"ontap"` or `"vantara"`)
- `datastore_id` - vSphere datastore ID (e.g., `"datastore-123"`)
- `default_vm_name` - Template/VM name configured with cloud-init
- `storage_hostname` - Storage array management hostname/IP
- `storage_username` - Storage array admin username
- `storage_password` - Storage array admin password

#### Clone Method Configuration

**For SSH method** (simpler, recommended):

- `esxi_clone_method: "ssh"`
- `esxi_host` - ESXi hostname/IP
- `esxi_user` - ESXi SSH username (typically `root`)
- `esxi_password` - ESXi SSH password

**For VIB method** (requires VIB pre-installation):

- `esxi_clone_method: "vib"` (or omit, as it's the default)

#### Vendor-Specific Fields

**Currently implemented in test suite:**

- **NetApp ONTAP** (`storage_vendor_product: "ontap"`):
  - `ontap_svm` - SVM/vServer name (required)
- **Hitachi Vantara** (`storage_vendor_product: "vantara"`):
  - No additional fields required

> **Note**: While the copy-offload feature supports additional storage vendors (Pure Storage, Dell PowerMax/PowerFlex,
> HPE, Infinidat, IBM FlashSystem), vendor-specific configuration for these is not yet implemented in this test suite.
> Contributions welcome!

#### Multi-Datastore Support (Advanced)

For VMs with disks distributed across multiple datastores on the same storage array:

- `datastore_id` - Primary/default datastore for VM base disks (required)
- `secondary_datastore_id` - Secondary datastore on the same storage system for additional disks
  (⚠️ **Future**: Not yet fully implemented in test suite)

**example**: The `test_copyoffload_multi_disk_different_path_migration` test will use this feature to
validate multi-datastore migrations.

#### RDM (Raw Device Mapping) Support (Advanced)

For testing RDM virtual disk migrations:

- `rdm_lun_uuid` - UUID of the RDM LUN to use for RDM virtual disk tests (optional)

**example**: The `test_copyoffload_rdm_virtual_disk_migration` test uses this feature to validate
migration of VMs with RDM virtual disks.

---

### Running Copy-Offload Tests

Copy-offload tests are designed to run as **OpenShift Jobs** for long-running migrations. Follow these steps:

#### Step 1: Create Secret with Configuration

Store your `.providers.json` file as an OpenShift secret:

```bash
oc create namespace mtv-tests
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  -n mtv-tests
```

#### Step 2: Create and Run Job

Use this template to run copy-offload tests. Customize the placeholders:

- `[JOB_NAME]` - Unique job name (e.g., `mtv-copyoffload-tests`)
- `[TEST_MARKERS]` - Pytest marker (`copyoffload`)
- `[TEST_FILTER]` - Optional: specific test name for `-k` flag (omit lines for all tests)

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
        image: ghcr.io/redhatqe/mtv-api-tests:latest  # Or use your custom image from Quick Start Step 1 Option B
        command:
          - uv
          - run
          - pytest
          - -m
          - [TEST_MARKERS]
          # Optional: Add these two lines to run a specific test
          # - -k
          # - [TEST_FILTER]
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

##### Example 1: Run all copy-offload tests

Replace placeholders:

- `[JOB_NAME]` → `mtv-copyoffload-tests`
- `[TEST_MARKERS]` → `copyoffload`
- Remove the commented `-k` and `[TEST_FILTER]` lines

##### Example 2: Run a specific test

Replace placeholders:

- `[JOB_NAME]` → `mtv-copyoffload-thin-test`
- `[TEST_MARKERS]` → `copyoffload`
- Uncomment `-k` and `[TEST_FILTER]`, replace `[TEST_FILTER]` → `test_copyoffload_thin_migration`

**Replace cluster configuration:**

- `ghcr.io/redhatqe/mtv-api-tests:latest` - Use this public image, or substitute with your custom image
  if you built one in Quick Start Step 1 Option B (e.g., `<YOUR-REGISTRY>/mtv-tests:latest`)
- `api.your-cluster.com` - Your OpenShift cluster API endpoint
- `kubeadmin` / `your-cluster-password` - Your cluster credentials
- `8.0.3.00400` - Your vSphere version (must match key in `.providers.json`)
- `ontap-san-block` - Your OpenShift storage class name

**Available test names** (for use with `-k` filter):

- `test_copyoffload_thin_migration` - Thin provisioned disk migration
- `test_copyoffload_thick_lazy_migration` - Thick lazy zeroed disk migration
- `test_copyoffload_multi_disk_migration` - Multi-disk VM migration
- `test_copyoffload_multi_disk_different_path_migration` - Multi-disk with different paths
- `test_copyoffload_rdm_virtual_disk_migration` - RDM virtual disk migration

#### Step 3: Monitor Test Execution

**Follow test logs in real-time**:

```bash
oc logs -n mtv-tests job/mtv-copyoffload-tests -f
```

**Check Job status**:

```bash
oc get jobs -n mtv-tests
# Look for "COMPLETIONS" showing 1/1 = success, 0/1 = still running
```

**Retrieve test results**:

```bash
# Copy JUnit XML report from completed pod
POD_NAME=$(oc get pods -n mtv-tests -l job-name=mtv-copyoffload-tests -o jsonpath='{.items[0].metadata.name}')
oc cp mtv-tests/$POD_NAME:/app/junit-report.xml ./junit-report.xml
```

**Clean up after tests**:

```bash
oc delete job mtv-copyoffload-tests -n mtv-tests
```

---

## Useful Test Options

### Debug and Troubleshooting Flags

Add these flags to any test run (Podman, Docker, or local) for debugging:

```bash
# Enable verbose output
pytest -v                      # Verbose test names

# Enable debug logging
pytest -s -vv                  # Very verbose with output capture disabled

# Set MTV/OpenShift debug level
export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG
podman run -e OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG ...

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

**List all available test names** (run inside the container):

```bash
podman run --rm ghcr.io/redhatqe/mtv-api-tests:latest uv run pytest --collect-only -q
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
# Mount a volume to save the report
podman run --rm \
  -v $(pwd)/.providers.json:/app/.providers.json:ro \
  -v $(pwd)/results:/app \
  ghcr.io/redhatqe/mtv-api-tests:latest \
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
POD_NAME=$(oc get pods -n mtv-tests -l job-name=mtv-copyoffload-tests -o jsonpath='{.items[0].metadata.name}')
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
A: No. Everything runs inside the container. You only need Podman or Docker.

**Q: How long do tests take?**
A: Test duration varies. tier0 tests are fastest (smoke tests), warm migration tests include warm migration
scenarios, and copy-offload tests are optimized for speed with shared storage.

**Q: Can I run on SNO (Single Node OpenShift)?**
A: Yes. SNO has been validated with copy-offload tests. Other test types may work but have not
been specifically validated on SNO.

**Q: What's the difference between Podman/Docker run and OpenShift Job?**
A: Podman/Docker run uses local `.providers.json`. Job uses OpenShift secret and runs inside the cluster.

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
