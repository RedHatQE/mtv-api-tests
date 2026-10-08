# Supported Providers And Scenarios

`mtv-api-tests` is an integration test suite for real MTV migrations into OpenShift Virtualization. The support matrix in this repository comes from three places: provider profiles
in `.providers.json`, scenario definitions in `tests/tests_config/config.py`, and pytest markers in `pytest.ini`. Collection-time skipping in `conftest.py` then narrows the matrix
further, based on the provider you actually configure.

## Supported Source Providers

The `source_provider` setting points to a named profile in `.providers.json`. That means you can keep multiple profiles for the same platform, such as different vSphere versions or
labs, and select the one you want by profile name.

```2:17:.providers.json.example
  "$schema": "https://raw.githubusercontent.com/RedHatQE/mtv-api-tests/main/providers_schema.json",
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
    "luks_passphrase": "LUKS DISK ENCRYPTION PASSPHRASE",  # pragma: allowlist secret
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>",
    "endpoint_type": "vcenter",
  },
```

Copy-offload is the same provider type with an extra section:

```19:30:.providers.json.example
  "vsphere-copy-offload": {
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
    "copyoffload": {
```

The shipped example file also includes `vsphere-esxi`, `ovirt`, `openstack`, `openshift`, `ova`, and `hyperv` profiles.

The `type` values come from the Forklift provider-type constants: `vsphere`, `openshift`, `ovirt`, `ova`, `openstack`, and
`hyperv`. The provider adapters in `libs/providers/` map one-to-one onto them.

| Source provider | `type` value | Cold migration | Warm migration | Copy-offload | Notes |
| --- | --- | --- | --- | --- | --- |
| VMware vSphere (vCenter) | `vsphere` | Yes | Yes | Yes | This is the broadest coverage area in the suite. Copy-offload is a vSphere profile with an extra `copyoffload` section. |
| VMware ESXi | `vsphere` with `"endpoint_type": "esxi"` | Yes | Yes | No | A standalone host cannot clone, so `clone_provider` names the vCenter profile that clones. |
| RHV / oVirt | `ovirt` | Yes | Yes | No | Cold, warm, comprehensive, and post-hook scenarios exist for RHV. |
| OpenStack | `openstack` | Yes | No | No | Warm tests are skipped for this provider family. |
| OpenShift | `openshift` | Yes | No | No | Supported as a source provider for OCP-to-OCP migrations, but not for warm scenarios. |
| OVA | `ova` | Yes | No | No | Cold-style scenarios only. OVA VMs are never cloned; the destination name gets a session UUID prefix. |
| Hyper-V | `hyperv` | Yes | No | No | Cold and comprehensive scenarios, including static IP preservation checks. |

> **Note:** `vsphere-copy-offload` and `vsphere-esxi` are not separate provider families. Both are `type: "vsphere"`; the difference is configuration, not provider type.

> **Warning:** Warm migration is not available for every source provider. The collection hook in `conftest.py` skips every `warm` item when the configured source provider cannot do
> warm migrations.

```340:349:conftest.py
            warm_unsupported = (
                Provider.ProviderType.OPENSTACK,
                Provider.ProviderType.OPENSHIFT,
                Provider.ProviderType.OVA,
                Provider.ProviderType.HYPERV,
            )
            if source_provider_type in warm_unsupported:
                warm_skip = pytest.mark.skip(reason=f"{source_provider_type} warm migration is not supported.")
                for item in items:
                    if "warm" in item.keywords:
```

Copy-offload, shared-disk, deep-inspection, AAP hook, and LUKS tests are vSphere-only, and `ca_crt` tests are skipped for
`openshift` and `ova` sources, because those providers do not carry a CA certificate in their provider secret.
A separate branch of the same hook enforces that:

```362:372:conftest.py
            ca_cert_unsupported = (
                Provider.ProviderType.OPENSHIFT,
                Provider.ProviderType.OVA,
            )
            if source_provider_type in ca_cert_unsupported:
                ca_cert_skip = pytest.mark.skip(
                    reason=f"{source_provider_type} does not use CA certificates in provider secrets"
                )
                for item in items:
                    if "ca_crt" in item.keywords:
                        item.add_marker(ca_cert_skip)
```

```353:359:conftest.py
            if source_provider_type != Provider.ProviderType.VSPHERE:
                vsphere_only_skip = pytest.mark.skip(reason="Test is only applicable to vSphere source providers")
                for item in items:
                    if any(
                        kw in item.keywords for kw in ("copyoffload", "shared_disk", "deep_inspection", "aap", "luks")
                    ):
                        item.add_marker(vsphere_only_skip)
```

A third capability gate of the same hook covers `add_nic`, which is a plan config flag rather than a marker, so it resolves
each item's parametrized plan config:

```374:383:conftest.py
            # Skip tests whose plan config requests `add_nic` on a non-vSphere provider.
            # `add_nic` is a plan config flag, not a marker, so resolve each item's config.
            if source_provider_type != Provider.ProviderType.VSPHERE:
                add_nic_skip = pytest.mark.skip(
                    reason=f"add_nic is vSphere-only; skipping for provider '{source_provider_type}'"
                )
                for item in items:
                    test_config = _resolve_item_plan_config(item) or {}
                    if any(vm.get("add_nic") for vm in test_config.get("virtual_machines", [])):
                        item.add_marker(add_nic_skip)
```

## Migration Modes

This suite exercises three practical migration modes:

- Cold migration: the default path, represented by scenarios where `warm_migration` is `False`.
- Warm migration: the precopy and cutover path, represented by scenarios where `warm_migration` is `True`.
- Copy-offload: the accelerated vSphere path, represented by scenarios where `copyoffload` is `True`.

A simple warm scenario in the built-in test matrix looks like this:

```17:26:tests/tests_config/config.py
    "test_sanity_warm_mtv_migration": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": True,
        "preserve_static_ips": True,
```

There is no separate `cold` marker. In this repository, "cold" is the normal case when `warm_migration` is not enabled.

### Copy-offload specifics

Copy-offload is the most specialized mode in the suite. It is vSphere-only, and the repository includes both cold copy-offload coverage and a dedicated warm copy-offload scenario.

The accepted `storage_vendor_product` values are defined in `utilities/copyoffload_constants.py`:

- `ontap`
- `vantara`
- `primera3par`
- `pureFlashArray`
- `powerflex`
- `powermax`
- `powerstore`
- `infinibox`
- `flashsystem`

`storage_vendor_product` and `datastore_id` are always required, and base copy-offload storage credentials are always required:

- `storage_hostname`
- `storage_username`
- `storage_password`

Some vendors also require extra fields:

- `ontap`: `ontap_svm`
- `vantara`: `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list`
- `pureFlashArray`: `pure_cluster_prefix`
- `powerflex`: `powerflex_system_id`
- `powermax`: `powermax_symmetrix_id`
- `primera3par`, `powerstore`, `infinibox`, `flashsystem`: no extra vendor-specific fields beyond the base storage credentials

Optional fields unlock specific scenarios:

- `secondary_datastore_id`: required by the multi-datastore tests
- `non_xcopy_datastore_id`: required by the mixed XCOPY/non-XCOPY datastore test
- `dedicated_migration_hosts`: required by the dedicated-migration-host tests, which set `StorageMap` `offloadPlugin.vsphereXcopyConfig.dedicatedMigrationHosts`
- `rdm_lun_uuid`: LUN used by the RDM disk tests
- `storage_secret_extra`: extra Secret `stringData` keys not covered by the vendor fields, or the `COPYOFFLOAD_STORAGE_SECRET_EXTRA` environment variable

If you use SSH-based ESXi cloning for copy-offload, the suite also expects `esxi_clone_method: "ssh"` plus `esxi_host`, `esxi_user`, and `esxi_password`.

> **Tip:** Copy-offload credentials can come from `.providers.json` or from `COPYOFFLOAD_...` environment variables. Environment variables win, which is useful when you do not want
> secrets stored in files.

```98:123:utilities/copyoffload_migration.py
def get_copyoffload_credential(
    credential_name: str,
    copyoffload_config: dict[str, Any],
) -> str | None:
    """
    Get a copyoffload credential from environment variable or config file.

    Environment variables take precedence over config file values.
    Environment variable names are constructed as COPYOFFLOAD_{credential_name.upper()}.

    Args:
        credential_name: Name of the credential (e.g., "storage_hostname", "ontap_svm",
                        "vantara_hostgroup_id_list")
        copyoffload_config: Copyoffload configuration dictionary

    Returns:
        str | None: Credential value from env var or config, or None if not found

    Examples:
        - "storage_hostname" → "COPYOFFLOAD_STORAGE_HOSTNAME"
        - "ontap_svm" → "COPYOFFLOAD_ONTAP_SVM"
        - "vantara_hostgroup_id_list" → "COPYOFFLOAD_VANTARA_HOSTGROUP_ID_LIST"
    """
    env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
    return os.getenv(env_var_name) or copyoffload_config.get(credential_name)
```

### Plan flags that shape source-side preparation

Several scenario keys do not appear in the `Plan` CR. They change how the suite prepares the source side before the migration starts:

| Plan flag | What it does |
| --- | --- |
| `rdm_as_lun` | Maps RDM disks as LUN devices on the SCSI bus instead of as virtual disks. Used by the RDM copy-offload scenarios. |
| `per_nic_network_map` | Creates one `NetworkMap` entry per NIC instead of deduplicating identical mappings. |
| `clone_to_same_host` | Pins every VM after the first to the ESXi host used for the first clone. Required for per-host inflight throttling. |
| `disable_drs_for_vms` | Applies a per-VM DRS override after cloning so vCenter cannot relocate the VM. Requires a VMware clone provider, and is rejected for OVA sources. |
| `pin_to_non_dedicated_host` | Resolves an ESXi host outside `dedicated_migration_hosts` and pins the clones to it, so dedicated-host verification can never pass by coincidence. |

## Pytest Markers

The repository defines these markers in `pytest.ini`:

```15:36:pytest.ini
markers =
    tier0: Core functionality tests (smoke tests)
    tier1: Extended functionality tests
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    copyoffload_sanity: Copy-offload sanity tests - core scenarios for quick validation
    copyoffload_snapshots: Copy-offload snapshot tests (vSphere only)
    shared_disk: Shared disk migration tests (vSphere only)
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))
    ova: OVA provider-specific tests
    vsphere: vSphere provider-specific tests
    esxi: ESXi provider-specific tests
    rhv: RHV provider-specific tests
    hyperv: Hyper-V provider-specific tests
    openstack: OpenStack provider-specific tests
    openshift: OpenShift provider-specific tests
    deep_inspection: Deep Inspection / Conversion CR tests (vSphere only)
    ca_crt: CA certificate field (ca.crt) provider secret tests
    aap: AAP (Ansible Automation Platform) hook integration tests (vSphere only)
    upgrade: MTV operator upgrade tests
```

In practice:

- `tier0` is the smoke or core regression slice, and `tier1` covers extended functionality.
- `warm`, `remote`, and `copyoffload` are the main user-facing selectors for scenario families.
- `copyoffload_sanity` selects the curated quick copy-offload subset, and `copyoffload_snapshots` selects the snapshot-based copy-offload classes.
- `shared_disk`, `deep_inspection`, `aap`, and `ca_crt` select feature suites that have no single-mode marker of their own.
- `vsphere`, `esxi`, `rhv`, `hyperv`, `openstack`, `openshift`, and `ova` mark which providers a test is written for. They also drive collection-time skipping.
- `upgrade` selects the MTV operator upgrade scenarios.
- `incremental` is execution behavior, not a functional feature area. These class-based tests move step by step through
  StorageMap, NetworkMap, Plan, migration execution, and validation.
- `min_mtv_version` is available when a scenario needs a newer MTV version.

> **Note:** `comprehensive` is a scenario family in this repository, not a pytest marker. You select it by file or class, not with `-m comprehensive`.

> **Note:** Remote scenarios are opt-in. They are skipped unless `remote_ocp_cluster` is set.

## Major Scenario Families

| Family | How you identify it | What it covers |
| --- | --- | --- |
| Tier0 | `@pytest.mark.tier0` | Core smoke coverage: sanity cold and warm, comprehensive cold and warm, OVA, shared-disk RHEL, cluster-role warm, post-hook failure |
| Tier1 | `@pytest.mark.tier1` | Extended coverage: XFS, LUKS, dual-NIC, CA certificate, insecure skip-verify, deep inspection, AAP hooks, shared-disk Windows |
| Warm | `@pytest.mark.warm` | Standard warm flows, a 2-disks/2-NICs case, remote warm, comprehensive warm, XFS warm, plan-driven deep inspection, warm copy-offload |
| Remote | `@pytest.mark.remote` | Remote OpenShift destination scenarios for cold, warm, and cluster-role flows |
| Comprehensive | Dedicated files and classes | Plan options such as static IP preservation, custom VM namespaces, PVC naming templates, labels, affinity, node placement |
| Copy-offload | `@pytest.mark.copyoffload` | XCOPY coverage across disk types, snapshots, datastores, RDM disks, naming, dedicated migration hosts, scale, concurrency |
| Shared disk | `@pytest.mark.shared_disk` | `migrateSharedDisks` ownership between an owner VM and a consumer VM, for RHEL and Windows guests |
| Deep inspection | `@pytest.mark.deep_inspection` | The `Conversion` CR lifecycle: standalone, cancel, validation conditions, and plan-driven concerns blocking migration |
| LUKS | `tests/luks/` plus `-k luks` | LUKS-encrypted disk migration, plus the expected failure path with a wrong passphrase |
| XFS | `xfs_compatibility` scenario key | XFS v4 compatibility for cold and warm migrations, verified with `xfs_info` in the guest |
| Hooks | `tests/hooks/` | A pre-hook success plus post-hook failure scenario, and an AAP-based hook scenario |
| Plan lifecycle | `tests/plan_lifecycle/` | PVC and DataVolume cleanup after a failed plan is archived |
| Upgrade | `@pytest.mark.upgrade` | Migration resources created pre-upgrade, migrated after the MTV operator upgrade |

Scenario families are intentionally allowed to overlap. For example, comprehensive warm coverage is both `tier0` and `warm`,
warm copy-offload belongs to both `warm` and `copyoffload`, and the sanity warm class carries `tier0`, `warm`, and `upgrade`.

### Tier0 coverage

The `tier0` slice in this repository is broader than a single smoke test. It includes:

- `TestSanityColdMtvMigration`
- `TestSanityWarmMtvMigration`
- `TestColdMigrationComprehensive`
- `TestWarmMigrationComprehensive`
- `TestOvaColdMigration`
- `TestSharedDiskRhelMigration`
- `TestClusterroleWarmWithSccMigration`
- `TestPostHookRetainFailedVm`

Two of those need something extra before they run: the OVA class needs an `ova` source provider, and
`TestClusterroleWarmWithSccMigration` is remote and is skipped without `remote_ocp_cluster`.
The last case matters because it covers a failure path: the migration is expected to fail in the post-hook stage while
the migrated VMs are retained for verification.

### Remote coverage

The built-in remote scenarios are:

- `TestColdRemoteOcp`
- `TestWarmRemoteOcp`
- `TestClusterroleWarmMtvMigration`
- `TestClusterroleWarmWithSccMigration`

These are gated by the `remote` marker and the `remote_ocp_cluster` setting. If you do not provide that setting, pytest skips them instead of failing later during migration setup.

### Comprehensive coverage

The comprehensive tests are the best place to look when you want end-to-end coverage of plan options beyond "can the VM migrate."

A warm comprehensive scenario is configured like this:

```665:703:tests/tests_config/config.py
    "test_warm_migration_comprehensive": {
        "virtual_machines": [
            {
                "name": "mtv-win2022-ip-3disks",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": True,
        "target_power_state": "on",
        "preserve_static_ips": True,
        "enable_nested_virtualization": False,
        "vm_target_namespace": f"mtv-vms-warm-comprehensive-{uuid.uuid4().hex[:4]}",
        "multus_namespace": "default",  # Cross-namespace NAD access
        # Keys must be Provider.ProviderType string values (e.g. "vsphere") or "default".
        # vsphere uses .FileName; default truncates VmName to stay within 63 chars.
        "pvc_name_template": {
            "vsphere": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
            "default": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
        },
        "pvc_name_template_use_generate_name": True,
        "target_labels": {
            "mtv-comprehensive-test": None,  # None = auto-generate with session_uuid
            "static-label": "static-value",
        },
        "target_affinity": {
            "podAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"app": "comprehensive-test"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                        "weight": 75,
                    }
                ]
            }
        },
        "guest_agent_timeout": 600,
    },
```

The cold comprehensive scenario follows the same idea, but adds cold-specific scheduling and naming checks such as
`target_node_selector`, fixed PVC naming, and `warm_migration: False`.

### Copy-offload coverage

`tests/copyoffload/test_copyoffload_migration.py` is the largest single scenario matrix in the repository. It covers:

- Thin, thick-lazy, and thick-eager disk migrations
- Snapshot-based cases, including a 2 TB VM with snapshots
- Multi-disk and multi-datastore layouts
- Mixed XCOPY and non-XCOPY datastore behavior, including fallback paths
- RDM virtual disks and RDM physical disks, cold and warm
- Independent persistent and independent nonpersistent disks
- A VM with 10 mixed thin and thick disks
- Nonconforming source VM names
- Dedicated migration hosts, including the invalid-host-id failure case
- Populator and VM inflight throttling
- Warm copy-offload
- Scale coverage with 5 VMs in one run
- Simultaneous copy-offload plans
- Concurrent XCOPY and VDDK plans in the same suite

`copyoffload_sanity` selects a seven-class subset for quick validation: thick-eager snapshots, RDM virtual disk,
multi-datastore, mixed datastore, 10 mixed disks, warm copy-offload, and the 5-VM scale run.

> **Warning:** Copy-offload is intentionally strict about prerequisites. The suite fails early if the source provider is not vSphere or if required copy-offload fields are missing.

### Feature suites worth knowing about

- **LUKS.** `tests/luks/` migrates a LUKS-encrypted VM and then verifies `crypto_LUKS` from inside the migrated guest. A
  second class feeds a wrong passphrase and expects the migration to fail. The passphrase comes from `luks_passphrase` in
  `.providers.json`, with a per-VM override in the scenario.
- **XFS.** `tests/cold/test_cold_migration_xfs.py` and `tests/warm/test_warm_migration_xfs.py` set `xfs_compatibility: True`
  and verify the guest filesystem with an `xfs_check` command, expecting `crc=0` in `xfs_info` output.
- **Shared disk.** `tests/shared_disk/` migrates an owner VM with `migrateSharedDisks: True` and a consumer VM with it
  `False`, then verifies the shared disk data on both.
- **Deep inspection.** `tests/deep_inspection/` drives Forklift `Conversion` resources: standalone conversion with cancel
  and validation-condition cases, plus a plan-driven case where inspection concerns must block a warm migration.
- **Plan lifecycle.** `tests/plan_lifecycle/test_plan_archive_pvc_cleanup.py` fails a plan in the post-hook stage, archives
  it, and waits for the leftover PVCs and DataVolumes to disappear.
- **Upgrade.** `tests/upgrade/test_upgrade_migration.py` creates StorageMap, NetworkMap, and Plan on the pre-upgrade
  operator, upgrades MTV from an external upgrade repository, and then migrates. It needs `upgrade_repo_url`,
  `upgrade_repo_ref`, `upgrade_script_path`, and the target version settings.
- **Hooks and AAP.** `tests/hooks/` covers a pre-hook that must succeed followed by a post-hook that must fail, with the
  migrated VMs retained for verification, plus an AAP-based hook scenario where the suite installs or uses an AAP
  deployment, syncs the test playbooks project, and creates the hook.

## Practical Tip

> **Tip:** Before running live migrations, use `uv run pytest --collect-only` to confirm what your current provider profile and markers will collect. The repository already uses
> that pattern in `tox.toml`, and the container image defaults to it.

```4:18:tox.toml
[env.pytest-check]
commands = [
  [
    "uv",
    "run",
    "pytest",
    "--setup-plan",
  ],
  [
    "uv",
    "run",
    "pytest",
    "--collect-only",
  ],
]
```

If you only remember one rule for this page, make it this: vSphere has the broadest coverage, copy-offload is vSphere-only,
warm migration is not supported for every provider, and `tier0`, `tier1`, `warm`, `remote`, `copyoffload`, `shared_disk`,
`deep_inspection`, and `upgrade` are the markers you will use most often to slice the suite.
