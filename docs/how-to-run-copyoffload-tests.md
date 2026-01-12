# Copy-Offload: Accelerated Migrations Guide

**What is copy-offload?** Copy-offload is an MTV feature that uses the storage backend to directly copy
VM disks from vSphere datastores to OpenShift PVCs using XCOPY operations, bypassing the traditional v2v
transfer path. This requires shared storage infrastructure between vSphere and OpenShift, VAAI (vSphere APIs
for Array Integration) enabled on ESXi hosts, and a configured StorageMap with offload plugin settings.

For technical implementation details, see the
[vsphere-xcopy-volume-populator documentation](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator).

---

## Prerequisites

Before running copy-offload tests, ensure your environment meets these requirements:

### 1. **VMware Environment**

- **ESXi + vCenter** (recommended) or standalone ESXi
- **Clone method configured**: Choose either VIB or SSH method
  - **VIB**: Requires pre-installing VMware Installation Bundle on ESXi hosts
  - **SSH**: Requires SSH access to ESXi hosts (simpler setup)
  - See setup guide: [Clone Methods (VIB vs SSH)](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#clone-methods-vib-vs-ssh)

### 2. **Shared Storage Configuration**

- **Tested storage vendors**: These tests currently support:
  - ✅ **NetApp ONTAP** (fully implemented with vendor-specific fields)
  - ✅ **Hitachi Vantara** (validated, no additional vendor-specific fields required)
- **Additional vendors supported by copy-offload feature** (not yet validated in test suite):
  - Pure Storage, Dell (PowerMax/PowerFlex/PowerStore), HPE Primera/3PAR, Infinidat, IBM FlashSystem
  - Full vendor list: [Supported Storage Providers](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#supported-storage-providers)
- **Storage type**: Must be SAN/Block (iSCSI or FC) - **NFS is not supported** for xcopy
- **Configuration**: Same physical storage accessible from both VMware and OpenShift
  - Use matching configurations (e.g., same NetApp SVM for both environments)

### 3. **OpenShift Environment**

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

### 4. **Test VM with Cloud-Init**

Create a VM in vSphere with:

- SSH access (root user)
- Serial console enabled
- Network connectivity
- Pre-configured disks with test data

A custom cloud-init script for automated VM provisioning is in development. For now, manually create a VM
meeting the above requirements and use its name as `default_vm_name` in your configuration.

### 5. **OpenShift Permissions**

Ensure your OpenShift user has permissions to create MTV resources and VMs.

<details>
<summary><strong>Option 1: Cluster Admin (Quick Setup)</strong></summary>

If using `oc` CLI for testing purposes:

```bash
oc adm policy add-cluster-role-to-user cluster-admin $(oc whoami)
```

**Note**: This grants full cluster administrator access. For production or external partner access, use the
minimum RBAC permissions below instead.

</details>

<details>
<summary><strong>Option 2: Minimum RBAC Permissions </strong></summary>

For least-privilege access, grant only the permissions required for MTV copy-offload testing:

**Required Permissions:**

- **Namespaces**: Create, delete test namespaces
- **MTV Resources**: Full access to Plan, Provider, NetworkMap, StorageMap CRDs in `openshift-mtv`
- **KubeVirt Resources**: Full access to VirtualMachine, VirtualMachineInstance resources
- **PVCs and Storage**: Create, read, update, delete PersistentVolumeClaims
- **Secrets and ConfigMaps**: Create and read for test configuration
- **ServiceAccounts and RBAC**: Create ServiceAccounts, Roles, and RoleBindings in test namespaces

**Example Role and RoleBinding:**

Create a ClusterRole with minimum permissions:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: mtv-test-user
rules:
# Namespace management
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["create", "delete", "get", "list"]
# MTV resources
- apiGroups: ["forklift.konveyor.io"]
  resources: ["plans", "providers", "networkmaps", "storagemaps"]
  verbs: ["create", "delete", "get", "list", "update", "patch", "watch"]
- apiGroups: ["forklift.konveyor.io"]
  resources: ["plans/status", "providers/status"]
  verbs: ["get", "list", "watch"]
# KubeVirt VMs
- apiGroups: ["kubevirt.io"]
  resources: ["virtualmachines", "virtualmachineinstances"]
  verbs: ["create", "delete", "get", "list", "update", "patch", "watch"]
- apiGroups: ["kubevirt.io"]
  resources: ["virtualmachines/status", "virtualmachineinstances/status"]
  verbs: ["get", "list", "watch"]
# Storage
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["create", "delete", "get", "list", "update", "patch", "watch"]
- apiGroups: ["storage.k8s.io"]
  resources: ["storageclasses"]
  verbs: ["get", "list"]
# Secrets and ConfigMaps
- apiGroups: [""]
  resources: ["secrets", "configmaps"]
  verbs: ["create", "delete", "get", "list", "update", "patch"]
# ServiceAccounts and RBAC
- apiGroups: [""]
  resources: ["serviceaccounts"]
  verbs: ["create", "delete", "get", "list"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["create", "delete", "get", "list"]
# Pods (for log access and debugging)
- apiGroups: [""]
  resources: ["pods", "pods/log"]
  verbs: ["get", "list", "watch"]
```

Bind the role to your test ServiceAccount or user:

```bash
# Create the ClusterRole
oc apply -f mtv-test-user-clusterrole.yaml

# Bind to a user
oc create clusterrolebinding mtv-test-binding \
  --clusterrole=mtv-test-user \
  --user=<your-username>

# Or bind to a ServiceAccount for automated testing
oc create serviceaccount mtv-test-sa -n mtv-tests
oc create clusterrolebinding mtv-test-sa-binding \
  --clusterrole=mtv-test-user \
  --serviceaccount=mtv-tests:mtv-test-sa
```

</details>

---

## Configuration

Add the `copyoffload` section to your `.providers.json` file:

> **Note**: Replace `vsphere-8.0.3.00400` with your actual vSphere version (format: `vsphere-{version}`).
> The key and `version` field must match.
>
> ⚠️ **WARNING**: The JSON example below contains `# pragma: allowlist secret` comments for repository
> pre-commit hooks. These comments are **NOT valid JSON syntax** and will break JSON parsers. When copying
> this example to your `.providers.json` file, you must either:
>
> - **Remove lines containing** `# pragma: allowlist secret`, OR
> - **Replace sensitive values** with your actual credentials (removing the pragma comments)
>
> The pragma comments are only for documentation purposes in this repository and must not appear in your
> actual configuration file.

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

### Copy-offload Required Fields

- `storage_vendor_product` - Storage vendor product name (currently supported: `"ontap"` or `"vantara"`)
- `datastore_id` - vSphere datastore ID (e.g., `"datastore-123"`)
- `default_vm_name` - VM name configured with cloud-init for testing
- `storage_hostname` - Storage array management hostname/IP
- `storage_username` - Storage array admin username
- `storage_password` - Storage array admin password

### Clone Method Configuration

**For SSH method** (simpler, recommended):

- `esxi_clone_method: "ssh"`
- `esxi_host` - ESXi hostname/IP
- `esxi_user` - ESXi SSH username (typically `root`)
- `esxi_password` - ESXi SSH password

**For VIB method** (requires VIB pre-installation):

- `esxi_clone_method: "vib"` (or omit, as it's the default)

### Vendor-Specific Fields

**NetApp ONTAP** (`storage_vendor_product: "ontap"`):

- `ontap_svm` - SVM/vServer name (required for ONTAP)

**Hitachi Vantara** (`storage_vendor_product: "vantara"`):

- No vendor-specific fields required beyond the base storage configuration

> **Note**: While the copy-offload feature supports additional storage vendors (Pure Storage, Dell PowerMax/PowerFlex,
> HPE, Infinidat, IBM FlashSystem), vendor-specific configuration for these vendors is not yet available in this
> test suite. Contributions welcome!

### Multi-Datastore Support (Advanced)

For VMs with disks distributed across multiple datastores on the same storage array:

- `datastore_id` - Primary/default datastore for VM base disks (required)
- `secondary_datastore_id` - Secondary datastore on the same storage system for additional disks
  (⚠️ **Future**: Not yet fully implemented in test suite)

**example**: The `test_copyoffload_multi_disk_different_path_migration` test will use this feature to
validate multi-datastore migrations.

### RDM (Raw Device Mapping) Support (Advanced)

For testing RDM virtual disk migrations:

- `rdm_lun_uuid` - UUID of the RDM LUN to use for RDM virtual disk tests (optional)

**example**: The `test_copyoffload_rdm_virtual_disk_migration` test uses this feature to validate
migration of VMs with RDM virtual disks.

---

## Running Copy-Offload Tests

The recommended approach for running copy-offload tests is using **OpenShift Jobs**, which provides a consistent
and reliable execution environment. Follow these steps:

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
        image: ghcr.io/redhatqe/mtv-api-tests:latest  # Or use your custom image (see README Quick Start)
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
              --tc=source_provider_type:vsphere \
              --tc=source_provider_version:8.0.3.00400 \
              --tc=storage_class:ontap-san-block
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

### Example 1: Run all copy-offload tests

Replace placeholders:

- `[JOB_NAME]` → `mtv-copyoffload-tests`
- `[TEST_MARKERS]` → `copyoffload`
- Remove the commented `-k` and `[TEST_FILTER]` lines

### Example 2: Run a specific test

Replace placeholders:

- `[JOB_NAME]` → `mtv-copyoffload-thin-test`
- `[TEST_MARKERS]` → `copyoffload`
- Uncomment `-k` and `[TEST_FILTER]`, replace `[TEST_FILTER]` → `test_copyoffload_thin_migration`

**Replace cluster configuration:**

- `ghcr.io/redhatqe/mtv-api-tests:latest` - Use this public image, or substitute with your custom image
  if you built one from the README Quick Start section (e.g., `<YOUR-REGISTRY>/mtv-tests:latest`)
- If you used **Option A** (credentials in Job): Replace the command with explicit `--tc` flags as shown below
- If you used **Option B** (credentials in Secret): The Job template above automatically reads from the Secret
- `8.0.3.00400` - Your vSphere version (must match key in `.providers.json`)
- `ontap-san-block` - Your OpenShift storage class name

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
          - --tc=cluster_password:your-cluster-password
          - --tc=source_provider_type:vsphere
          - --tc=source_provider_version:8.0.3.00400
          - --tc=storage_class:ontap-san-block
```

⚠️ **Warning**: This exposes credentials in the Job spec visible via `oc get job -o yaml`.
Use Option B with Secret for better security.

**Example test names** (for use with `-k` filter):

- `test_copyoffload_thin_migration` - Thin provisioned disk migration
- `test_copyoffload_thick_lazy_migration` - Thick lazy zeroed disk migration
- `test_copyoffload_multi_disk_migration` - Multi-disk VM migration
- `test_copyoffload_multi_disk_different_path_migration` - Multi-disk with different paths
- `test_copyoffload_rdm_virtual_disk_migration` - RDM virtual disk migration

> **Note**: Additional copy-offload tests are being developed and automated. Use `pytest --collect-only -m copyoffload`
> to see the full list of available tests.

### Step 3: Monitor Test Execution

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

## Troubleshooting

### Storage Connection Issues

If tests fail with storage connection errors:

1. Verify storage credentials in `.providers.json`
2. Check network connectivity from OpenShift to storage array
3. Validate storage CSI driver installation: `oc get pods -n <csi-driver-namespace>`
4. Review CSI driver logs for errors

### Clone Method Issues

**SSH method**:

- Verify SSH access: `ssh root@esxi-host.example.com`
- Check ESXi firewall allows SSH connections
- Validate ESXi credentials

**VIB method**:

- Verify VIB is installed on all ESXi hosts
- Check VIB version compatibility with copy-offload feature

### StorageMap Configuration

Ensure your StorageMap matches your storage configuration:

```bash
oc get storagemap -n openshift-mtv -o yaml
```

Verify the `source` and `destination` storage class mappings are correct.

### Collect Debug Information

For copy-offload-specific issues:

```bash
# Check MTV operator logs
oc logs -n openshift-mtv deployment/forklift-controller --tail=100

# Check volume populator logs
oc logs -n openshift-mtv -l app=vsphere-xcopy-volume-populator --tail=100

# Check migration plan status
oc get plan -n openshift-mtv <plan-name> -o yaml
```

---

## Additional Resources

- [MTV Documentation](https://access.redhat.com/documentation/en-us/migration_toolkit_for_virtualization/)
- [Copy-Offload Feature Documentation](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator)
- [Clone Methods Guide](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#clone-methods-vib-vs-ssh)
- [Supported Storage Providers](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#supported-storage-providers)
