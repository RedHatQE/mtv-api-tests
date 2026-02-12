# Copy-Offload: Accelerated Migrations Guide

**What is copy-offload?** Copy-offload is an MTV feature that uses the storage array to directly copy
VM disks from vSphere datastores to OpenShift PVCs using offload operations, such as XCOPY, volume cow or
host based copy, bypassing the traditional v2v transfer path. This requires shared storage infrastructure
between vSphere and OpenShift, VAAI (vSphere APIs for Array Integration) enabled on ESXi hosts, and a
configured StorageMap with offload plugin settings.

For technical implementation details, see the
[vsphere-xcopy-volume-populator documentation](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator).

---

## Prerequisites

Before running copy-offload tests, ensure your environment meets these requirements:

### 1. **VMware Environment**

- **ESXi + vCenter** (recommended) or standalone ESXi
- **Clone method configured**: Choose either VIB or SSH method
  - **VIB**: Requires setting ESXi permissions to allow community-level VIB installation
  - **SSH**: Requires SSH access to ESXi hosts
  - See setup guide: [Clone Methods (VIB vs SSH)](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#clone-methods-vib-vs-ssh)

### 2. **Shared Storage Configuration**

- **Supported storage vendors**:
  - NetApp ONTAP
  - Hitachi Vantara
  - Pure Storage
  - Dell (PowerMax/PowerFlex/PowerStore)
  - HPE Primera/3PAR
  - Infinidat
  - IBM FlashSystem
  - Full vendor list: [Supported Storage Providers](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#supported-storage-providers)
- **Storage type**: Must be SAN/Block (iSCSI or FC) - **NFS is not supported at the moment** for copyoffload
- **Configuration**: Same physical storage array accessible from both VMware and OpenShift
  - For optimal copy-offload performance, use matching configurations when possible (e.g., same NetApp SVM for both environments)
  - If requirements aren't fully met, migration will fallback to standard transfer method

### 3. **OpenShift Environment**

- **CNV (OpenShift Virtualization)** installed
- **Storage Classes configured**:
  - **Block storage class**: Required for VM disk storage, must use vendor CSI driver (iSCSI or FC) connected to the same storage array as VMware
- **MTV (Migration Toolkit for Virtualization)** installed:
  - For versions before 2.11: Enable copy-offload by adding to ForkliftController spec:

    ```yaml
    spec:
      feature_copy_offload: 'true'
    ```

  - Configure storage secret in `openshift-mtv` namespace (see Configuration section below)
  - Configure StorageMap for copy-offload storage mapping (see [StorageMap Configuration](#storagemap-configuration))

### 4. **Test VM with Cloud-Init**

Create a VM in vSphere with:

- SSH access (root user)
- Serial console enabled
- Network connectivity
- Pre-configured disks with test data

**Note on VM preparation**: [WIP] Tests will run with a basic VM meeting the above requirements, but for meaningful
validation you'll need a VM properly configured with test disks and data. A complete guided setup document and
automation scripts for VM provisioning with cloud-init are currently in development and will be added to this
documentation.

### 5. **OpenShift Permissions**

The test suite requires permissions to create MTV resources (Providers, Plans, StorageMaps) and VirtualMachines across namespaces.

**For development/lab environments** (simplest approach):

> ⚠️ **WARNING**: The following command grants **full cluster-admin access**. Only use this in isolated
> test/development environments. Never use cluster-admin in production or shared clusters.

```bash
oc adm policy add-cluster-role-to-user cluster-admin $(oc whoami)
```

**For production/restricted environments** (recommended):

Work with your cluster administrator to grant the minimum required permissions:

- Create and manage VirtualMachines in test namespaces
- Create and manage resources in `openshift-mtv` namespace (Providers, Plans, StorageMaps)
- Read access to storage classes and PVCs

> **Note**: The exact RBAC requirements depend on your test scenarios. For least-privilege access, consult your
> cluster administrator to create a custom Role/RoleBinding with only the required permissions.

---

## Configuration

Add the `copyoffload` section to your `.providers.json` file:

> **Note: Provider Key Format**
>
> The provider key in `.providers.json` (e.g., `vsphere-8.0.3.00400`) is passed directly via
> `--tc=source_provider:vsphere-8.0.3.00400`. The value must match exactly what is defined as the
> key in your `.providers.json` file.
>
> **Action required**: Replace `vsphere-8.0.3.00400` in both the key name and `version` field below with your
> actual vSphere version.

**Configuration template:**

```json
{
  "vsphere-8.0.3.00400": {
    "type": "vsphere",
    "version": "8.0.3.00400",
    "fqdn": "vcenter.example.com",
    "api_url": "https://vcenter.example.com/sdk",
    "username": "administrator@vsphere.local",
    "password": "your-vcenter-password",  # pragma: allowlist secret
    "guest_vm_linux_user": "root",
    "guest_vm_linux_password": "your-vm-password",  # pragma: allowlist secret
    "copyoffload": {
      "storage_vendor_product": "ontap",
      "datastore_id": "datastore-123",
      "secondary_datastore_id": "datastore-456",
      "default_vm_name": "rhel9-cloud-init-template",
      "storage_hostname": "storage.example.com",
      "storage_username": "admin",
      "storage_password": "your-storage-password",  # pragma: allowlist secret
      "ontap_svm": "vserver-name",
      "esxi_clone_method": "ssh",
      "esxi_host": "esxi01.example.com",
      "esxi_user": "root",
      "esxi_password": "your-esxi-password",  # pragma: allowlist secret
      "rdm_lun_uuid": "naa.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    }
  }
}
```

> **Note**: The `# pragma: allowlist secret` comments are for this repository's security scanning and are **NOT valid JSON**.
> Remove these comments and replace placeholder values with your actual credentials when creating your `.providers.json` file.

### Copy-offload Required Fields

- `storage_vendor_product` - Storage vendor product name (see supported values in [Vendor-Specific Fields](#vendor-specific-fields) section)
- `datastore_id` - vSphere datastore ID where test VMs are cloned and stored (e.g., `"datastore-123"`)
  - Get via vSphere: **Datacenter → Storage → Datastore → Summary → More Objects ID**
- `default_vm_name` - VM name configured with cloud-init for testing
- `storage_hostname` - Storage array management hostname/IP
- `storage_username` - Storage array admin username
- `storage_password` - Storage array admin password

> **Important**: If using `secondary_datastore_id`, both datastores must be on the **same storage array** and support
> XCOPY/VAAI primitives for copy-offload to work. If this requirement isn't met, copy-offload will use fallback
> alternative transfer method.

### Clone Method Configuration

**For SSH method** (recommended):

- `esxi_clone_method: "ssh"`
- `esxi_host` - ESXi hostname/IP
- `esxi_user` - ESXi SSH username (typically `root`)
- `esxi_password` - ESXi SSH password

**For VIB method** (requires community-level VIB permissions):

- `esxi_clone_method: "vib"` (or omit, as it's the default)

### Vendor-Specific Fields

> **Important**: Only configure the fields for your selected `storage_vendor_product`. For example, if you're using
> NetApp ONTAP (`storage_vendor_product: "ontap"`), only configure `ontap_svm`. You may leave other vendor-specific
> fields blank or remove them from your configuration.

**NetApp ONTAP** (`storage_vendor_product: "ontap"`):

- `ontap_svm` - SVM/vServer name (required for ONTAP)

**Hitachi Vantara** (`storage_vendor_product: "vantara"`):

- `vantara_storage_id` - Storage array serial number
- `vantara_storage_port` - Storage API port (typically `443`)
- `vantara_hostgroup_id_list` - IO ports and host group IDs (format: `CL1-A,1:CL2-B,2:CL4-A,1:CL6-A,1`)

**Pure Storage FlashArray** (`storage_vendor_product: "pureFlashArray"`):

- `pure_cluster_prefix` - Pure cluster prefix (format: `px_a1b2c3d4`)
  - Get with: `printf "px_%.8s" $(oc get storagecluster -A -o=jsonpath='{.items[?(@.spec.cloudStorage.provider=="pure")].status.clusterUid}')`

**Dell PowerFlex** (`storage_vendor_product: "powerflex"`):

- `powerflex_system_id` - PowerFlex system ID
  - Get from vxflexos-config ConfigMap in vxflexos or openshift-operators namespace

**Dell PowerMax** (`storage_vendor_product: "powermax"`):

- `powermax_symmetrix_id` - PowerMax Symmetrix ID (format: `000123456789`)
  - Get from ConfigMap in powermax namespace used by CSI driver

**Dell PowerStore** (`storage_vendor_product: "powerstore"`):

- No vendor-specific fields required beyond the base storage configuration

**HPE Primera/3PAR** (`storage_vendor_product: "primera3par"`):

- No vendor-specific fields required beyond the base storage configuration

**Infinidat InfiniBox** (`storage_vendor_product: "infinibox"`):

- No vendor-specific fields required beyond the base storage configuration

**IBM FlashSystem** (`storage_vendor_product: "flashsystem"`):

- No vendor-specific fields required beyond the base storage configuration

### Multi-Datastore Support (Advanced)

Configuration for VMs with disks distributed across multiple datastores:

- `datastore_id` - Primary datastore where cloned test VMs are created (required for all tests)
- `secondary_datastore_id` - Secondary datastore on the same storage array for multi-datastore disk tests (optional)

> **Note**: If `secondary_datastore_id` is not provided, tests that require multi-datastore configurations
> (e.g., `test_copyoffload_multi_disk_different_path_migration`) will fail. Other copy-offload tests will
> continue to work normally.

### RDM (Raw Device Mapping) Support (Advanced)

Configuration for testing RDM virtual disk migrations:

- `rdm_lun_uuid` - UUID of the RDM LUN to use for RDM virtual disk tests (optional)

> **Note**: If `rdm_lun_uuid` is not provided, tests that require RDM virtual disks
> (e.g., `test_copyoffload_rdm_virtual_disk_migration`) will fail. Other copy-offload tests will
> continue to work normally.
>
> **Important**: RDM copy-offload is **currently supported only for Pure Storage**. Other storage vendors
> do not yet support copy-offload for RDM disks.
>
> **Important**: The `datastore_id` must be a **VMFS datastore** for RDM disk support. RDM disks are not
> supported on vSAN or NFS datastores.

---

## Running Copy-Offload Tests

The recommended approach for running copy-offload tests is using **OpenShift Jobs**, which provides a consistent
and reliable execution environment. Follow these steps:

### Step 1: Create Secret with Configuration

Create a secret containing:

- **`providers.json`** - Your provider configuration file
- **`cluster_host`** - OpenShift API endpoint (e.g., `https://api.your-cluster.com:6443`)
- **`cluster_username`** - OpenShift username (e.g., `kubeadmin`)
- **`cluster_password`** - OpenShift password

Example:

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

### Step 2: Create and Run Job

Use this example to run copy-offload tests:

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
EOF
```

**Customization notes:**

- Change `name: mtv-copyoffload-tests` to a unique job name if needed
- Replace `vsphere-8.0.3.00400` with your provider key from `.providers.json`
- Replace `my-block-storageclass` with your OpenShift block storage class name
- To run a specific test, add `-k test_name` after `-m copyoffload` (e.g., `-m copyoffload -k test_copyoffload_thin_migration`)
- To use a custom image, replace `ghcr.io/redhatqe/mtv-api-tests:latest` with your registry URL

The Job automatically reads cluster credentials from the Secret created in Step 1.

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

The test suite automatically cleans up resources it creates during test execution (VMs, migration plans, providers).
To preserve resources for debugging or log inspection, you can disable automatic cleanup by adding the `--skip-teardown`
flag to the pytest command in the Job definition:

```yaml
# In the Job command section, add --skip-teardown:
uv run pytest -m copyoffload --skip-teardown \
  -v \
  ...
```

After reviewing logs and resources, delete the Job and any test resources:

```bash
# Delete the Job (keeps pod and logs available)
oc delete job mtv-copyoffload-tests -n mtv-tests

# If you used --skip-teardown, manually clean up test resources:
# OpenShift resources:
oc delete vm --all -n <test-namespace>
oc delete plan --all -n openshift-mtv
oc delete provider <provider-name> -n openshift-mtv

# vSphere resources (cloned VMs):
# Delete cloned test VMs via vSphere UI or CLI
# Test VMs have names like: auto-<session-uuid>-<vm-name>
```

> **Note**: The Job pod remains available after completion until you delete the Job, allowing you to retrieve logs
> even after tests finish.

---

## Troubleshooting

### Storage Connection Issues

If tests fail with storage connection errors:

1. Verify storage credentials in the `mtv-test-config` secret (update and recreate the secret if needed)
2. Check network connectivity from OpenShift to storage array
3. Validate storage CSI driver installation: `oc get pods -n <csi-driver-namespace>`
4. Review CSI driver logs for errors

### Clone Method Issues

**SSH method**:

- Verify SSH access: `ssh root@esxi-host.example.com`
- Check ESXi firewall allows SSH connections
- Validate ESXi credentials

**VIB method**:

- VIB installation is automatic - verify ESXi host permissions allow community-level VIB installation
- Check the volume populator pod logs for permission or VIB installation errors:
  `oc logs -n openshift-mtv -l app=vsphere-xcopy-volume-populator`
- Refer to the [Clone Methods Guide](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#clone-methods-vib-vs-ssh)
  for VIB configuration requirements and troubleshooting

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
