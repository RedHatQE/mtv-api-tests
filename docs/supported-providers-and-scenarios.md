# Supported Providers And Scenarios

`mtv-api-tests` is an integration test suite for real MTV migrations into OpenShift Virtualization. The support matrix in this repository comes from three places: provider profiles in `.providers.json`, scenario definitions in `tests/tests_config/config.py`, and pytest markers in `pytest.ini`.

## Supported Source Providers

The `source_provider` setting points to a named profile in `.providers.json`. That means you can keep multiple profiles for the same platform, such as different vSphere versions or labs, and select the one you want by profile name.

```2:27:.providers.json.example
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
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
  },

  "vsphere-copy-offload": {
    "type": "vsphere",
    "version": "<SERVER VERSION>",
    "fqdn": "SERVER FQDN/IP",
    "api_url": "<SERVER FQDN/IP>/sdk",
    "username": "USERNAME",
    "password": "PASSWORD",  # pragma: allowlist secret
    // ... same guest credentials ...
    "copyoffload": {
```

The shipped example file also includes profiles for `ovirt`, `openstack`, `openshift`, and `ova`.

| Source provider | `type` value | Cold migration | Warm migration | Copy-offload | Notes |
| --- | --- | --- | --- | --- | --- |
| VMware vSphere | `vsphere` | Yes | Yes | Yes | This is the broadest coverage area in the suite. Copy-offload is implemented as a vSphere profile with an extra `copyoffload` section. |
| RHV / oVirt | `ovirt` | Yes | Yes, with extra suite gating | No | Standard MTV cold and warm scenarios exist in the codebase. |
| OpenStack | `openstack` | Yes | No | No | Warm tests explicitly skip this provider family. |
| OpenShift | `openshift` | Yes | No | No | Supported as a source provider, but not for warm scenarios. |
| OVA | `ova` | Yes | No | No | Supported for cold-style scenarios. The built-in OVA path uses a fixed imported VM name during plan preparation. |

> **Note:** `vsphere-copy-offload` is not a separate provider family. It is still `type: "vsphere"` with additional copy-offload settings.

> **Warning:** Warm migration is not available for every source provider. The warm test modules explicitly skip `openstack`, `openshift`, and `ova`.

```21:27:tests/test_mtv_warm_migration.py
pytestmark = [
    pytest.mark.skipif(
        _SOURCE_PROVIDER_TYPE
        in (Provider.ProviderType.OPENSTACK, Provider.ProviderType.OPENSHIFT, Provider.ProviderType.OVA),
        reason=f"{_SOURCE_PROVIDER_TYPE} warm migration is not supported.",
    ),
]
```

## Migration Modes

This suite exercises three practical migration modes:

- Cold migration: the default path, represented by scenarios where `warm_migration` is `False`.
- Warm migration: the precopy and cutover path, represented by scenarios where `warm_migration` is `True`.
- Copy-offload: the accelerated vSphere path, represented by scenarios where `copyoffload` is `True`.

A simple warm scenario in the built-in test matrix looks like this:

```16:25:tests/tests_config/config.py
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

There is no separate `cold` marker. In this repository, “cold” is the normal case when `warm_migration` is not enabled.

### Copy-offload specifics

Copy-offload is the most specialized mode in the suite. It is vSphere-only, and the repository includes both cold copy-offload coverage and a dedicated warm copy-offload scenario.

The accepted `storage_vendor_product` values in the code are:

- `ontap`
- `vantara`
- `primera3par`
- `pureFlashArray`
- `powerflex`
- `powermax`
- `powerstore`
- `infinibox`
- `flashsystem`

Base copy-offload storage credentials are always required:

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

If you use SSH-based ESXi cloning for copy-offload, the suite also expects `esxi_clone_method: "ssh"` plus `esxi_host`, `esxi_user`, and `esxi_password`.

> **Tip:** Copy-offload credentials can come from `.providers.json` or from `COPYOFFLOAD_...` environment variables. Environment variables win, which is useful when you do not want secrets stored in files.

```21:45:utilities/copyoffload_migration.py
def get_copyoffload_credential(
    credential_name: str,
    copyoffload_config: dict[str, Any],
) -> str | None:
    """
    Get a copyoffload credential from environment variable or config file.

    Environment variables take precedence over config file values.
    Environment variable names are constructed as COPYOFFLOAD_{credential_name.upper()}.
    """
    env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
    return os.getenv(env_var_name) or copyoffload_config.get(credential_name)
```

## Pytest Markers

The repository defines these markers in `pytest.ini`:

```17:23:pytest.ini
markers =
    tier0: Core functionality tests (smoke tests)
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))
```

In practice:

- `tier0` is the smoke or core regression slice.
- `warm`, `remote`, and `copyoffload` are the main user-facing selectors for scenario families.
- `incremental` is execution behavior, not a functional feature area. These class-based tests move step by step through StorageMap, NetworkMap, Plan, migration execution, and validation.
- `min_mtv_version` is available when a scenario needs a newer MTV version.

> **Note:** `comprehensive` is a scenario family in this repository, not a pytest marker. You select it by file or class, not with `-m comprehensive`.

> **Note:** Remote scenarios are opt-in. They are skipped unless `remote_ocp_cluster` is set.

## Major Scenario Families

| Family | How you identify it | What it covers |
| --- | --- | --- |
| Tier0 | `@pytest.mark.tier0` | Core smoke coverage: sanity cold, sanity warm, comprehensive cold, comprehensive warm, and the post-hook retention failure scenario |
| Warm | `@pytest.mark.warm` | Standard warm flows, a 2-disks/2-NICs case, remote warm, comprehensive warm, and warm copy-offload |
| Remote | `@pytest.mark.remote` | Remote OpenShift destination scenarios for both cold and warm flows |
| Comprehensive | Dedicated files and classes | Advanced plan options such as static IP preservation, custom VM namespaces, PVC naming templates, labels, affinity, and node placement |
| Copy-offload | `@pytest.mark.copyoffload` | XCOPY/offload coverage across disk types, snapshots, datastores, naming behavior, scale, and concurrency |

Scenario families are intentionally allowed to overlap. For example, comprehensive warm coverage is both `tier0` and `warm`, and warm copy-offload belongs to both `warm` and `copyoffload`.

### Tier0 coverage

The `tier0` slice in this repository is broader than a single smoke test. It includes:

- `TestSanityColdMtvMigration`
- `TestSanityWarmMtvMigration`
- `TestColdMigrationComprehensive`
- `TestWarmMigrationComprehensive`
- `TestPostHookRetainFailedVm`

That last case matters because it covers a failure path: the migration is expected to fail in the post-hook stage while the migrated VMs are retained for verification.

### Remote coverage

The built-in remote scenarios are:

- `TestColdRemoteOcp`
- `TestWarmRemoteOcp`

These are gated by the `remote` marker and the `remote_ocp_cluster` setting. If you do not provide that setting, pytest skips them instead of failing later during migration setup.

### Comprehensive coverage

The comprehensive tests are the best place to look when you want end-to-end coverage of plan options beyond “can the VM migrate.”

A warm comprehensive scenario is configured like this:

```434:465:tests/tests_config/config.py
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
        "vm_target_namespace": "custom-vm-namespace",
        "multus_namespace": "default",  # Cross-namespace NAD access
        "pvc_name_template": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
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
    },
```

The cold comprehensive scenario follows the same idea, but adds cold-specific scheduling and naming checks such as `target_node_selector`, fixed PVC naming, and `warm_migration: False`.

### Copy-offload coverage

`tests/test_copyoffload_migration.py` is the largest single scenario matrix in the repository. It covers:

- Thin and thick-lazy disk migrations
- Snapshot-based cases, including a 2 TB VM with snapshots
- Multi-disk and multi-datastore layouts
- Mixed XCOPY and non-XCOPY datastore behavior, including fallback paths
- RDM virtual disks
- Independent persistent and independent nonpersistent disks
- Nonconforming source VM names
- Warm copy-offload
- Scale coverage with 5 VMs in one run
- Simultaneous copy-offload plans
- Concurrent XCOPY and VDDK plans in the same suite

> **Warning:** Copy-offload is intentionally strict about prerequisites. The suite fails early if the source provider is not vSphere or if required copy-offload fields are missing.

## Practical Tip

> **Tip:** Before running live migrations, use `uv run pytest --collect-only` to confirm what your current provider profile and markers will collect. The repository already uses that pattern in `tox.toml`, and the container image defaults to it.

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

If you only remember one rule for this page, make it this: vSphere has the broadest coverage, copy-offload is vSphere-only, warm migration is not supported for every provider, and `tier0`, `warm`, `remote`, and `copyoffload` are the markers you will use most often to slice the suite.
