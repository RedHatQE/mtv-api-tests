# Provider Config File

`mtv-api-tests` keeps source provider definitions in `.providers.json`. Two shipped files describe it: `providers_schema.json` is a JSON Schema (draft 2020-12) that your editor
can validate against, and `.providers.json.example` is an annotated template with one entry per provider type.

At runtime the suite only parses JSON. It reads specific fields per provider type and ignores the rest, so the schema is the reference for what each provider entry may contain.

> **Warning:** `.providers.json.example` is an annotated template, not a ready-to-use `.providers.json`. The loader parses the file with `json.loads(...)`, so your real file
> must be strict JSON: remove comments, keep valid quoting, and avoid trailing commas.

> **Warning:** `.providers.json` usually contains provider passwords and guest OS passwords. Treat it as a secret file.

> **Note:** The top-level key is the provider name you select with `source_provider`. It does not have to match `type`. For example, the example file has a key named
> `vsphere-copy-offload`, but its `"type"` is still `"vsphere"`.

## How the file is located

`load_source_providers()` resolves the path in this order:

| Order | Source | Notes |
| --- | --- | --- |
| 1 | `--providers-json <path>` | Custom pytest option |
| 2 | `PROVIDERS_JSON_PATH` environment variable | CI-friendly override |
| 3 | `.providers.json` | Relative to the current working directory |

Fail-fast behavior:

- A path that does not exist raises `FileNotFoundError`.
- An empty or whitespace-only file raises `ProviderEmptyContentError`.
- A file whose top level is not a JSON object raises `ValueError`.
- `source_provider` must match one of the top-level keys, or provider lookups fail later.

## Top-Level Shape

```json
{
  "$schema": "https://raw.githubusercontent.com/RedHatQE/mtv-api-tests/main/providers_schema.json",
  "vsphere": { "type": "vsphere", "...": "..." }
}
```

- The root is an object. Every key except `$schema` is a provider definition.
- `$schema` is optional and is used only for editor validation.
- Each provider definition is validated against a `oneOf` block, one per provider type. Exactly one type block must match.

> **Warning:** The OpenShift provider block sets `additionalProperties: false`. Unknown keys in an `openshift` entry fail schema validation, even though the runtime loader
> ignores extra keys on other provider types.

## Fields By Provider Type

`type` selects the block. These tables match `providers_schema.json` exactly.

### `type: "vsphere"`

| Field | Required | Notes |
| --- | --- | --- |
| `type` | Yes | Constant `vsphere`. |
| `version` | Yes | Platform version label, for example `8.0.3`. Used in generated resource names. |
| `fqdn` | Yes | vCenter or ESXi host name or IP. |
| `api_url` | Yes | Provider API URL. vSphere expects a `/sdk` suffix. |
| `username` | Yes | vSphere login. |
| `password` | Yes | vSphere password. |
| `endpoint_type` | No | `vcenter` or `esxi`. Defaults to vCenter behavior when omitted. |
| `vddk_init_image` | No | VDDK init container image reference passed to the MTV `Provider`. |
| `clone_provider` | No | Name of a vCenter entry used for ESXi cloning. |
| `luks_passphrase` | No | Provider-level LUKS passphrase, used when a VM has no per-VM override. |
| `copyoffload` | No | Nested copy-offload block. See [vSphere copy-offload](#vsphere-copy-offload). |
| `guest_vm_linux_user` / `guest_vm_linux_password` | No | Linux guest SSH credentials. |
| `guest_vm_win_user` / `guest_vm_win_password` | No | Windows guest WinRM credentials. |

### `type: "ovirt"`

| Field | Required | Notes |
| --- | --- | --- |
| `type` | Yes | Constant `ovirt`. Use this even for an RHV source. |
| `version` | Yes | Platform version label. |
| `fqdn` | Yes | Engine host name or IP. Also used for the CA certificate download. |
| `api_url` | Yes | Engine API URL. Expect an `/ovirt-engine/api` suffix. |
| `username` | Yes | RHV login. |
| `password` | Yes | RHV password. |
| `guest_vm_linux_user` / `guest_vm_linux_password` | No | Linux guest SSH credentials. |
| `guest_vm_win_user` / `guest_vm_win_password` | No | Windows guest WinRM credentials. |

### `type: "openstack"`

| Field | Required | Notes |
| --- | --- | --- |
| `type` | Yes | Constant `openstack`. |
| `version` | Yes | Platform version label. |
| `fqdn` | Yes | Host name or IP. Also used for the CA certificate download. |
| `api_url` | Yes | Keystone v3 endpoint, for example `host:5000/v3`. |
| `username` | Yes | OpenStack login. |
| `password` | Yes | OpenStack password. |
| `user_domain_name` | Yes | Keystone user domain name. |
| `region_name` | Yes | Region name. |
| `project_name` | Yes | Project or tenant name. |
| `user_domain_id` | Yes | Keystone user domain UUID. |
| `project_domain_id` | Yes | Keystone project domain UUID. |
| `guest_vm_linux_user` / `guest_vm_linux_password` | No | Linux guest SSH credentials. |
| `guest_vm_win_user` / `guest_vm_win_password` | No | Windows guest WinRM credentials. |

### `type: "openshift"`

| Field | Required | Notes |
| --- | --- | --- |
| `type` | Yes | Constant `openshift`. |
| `version` | Yes | Platform version label. |
| `host` | Yes | Cluster API host name or URL. Required by the schema. |
| `api_url` | Yes | Cluster API URL. Required by the schema. |
| `username` | Yes | Required by the schema. |
| `password` | Yes | Required by the schema. |
| `storage_class` | Yes | Default storage class for target PVCs. Required by the schema. |
| `verify_ssl` | No | `true` or `false`. |
| `ca_bundle` | No | Path to a CA bundle file for SSL verification. |

This block is the strict one: `additionalProperties` is `false`, so no other keys are allowed.

> **Note:** The runtime path for an `openshift` entry does not authenticate with these values. The loader rewrites `api_url` to the current cluster host and reuses the
> session's own token secret, which is always created with `insecureSkipVerify: "true"`. See [OpenShift](#openshift).

### `type: "ova"`

| Field | Required | Notes |
| --- | --- | --- |
| `type` | Yes | Constant `ova`. |
| `version` | Yes | Platform version label. Any placeholder value works; it is used for naming only. |
| `fqdn` | Yes | Not used for OVA connections. |
| `api_url` | Yes | NFS share URL. This is the field the OVA provider actually consumes. |
| `username` | Yes | Not used for OVA connections. |
| `password` | Yes | Not used for OVA connections. |
| `guest_vm_linux_user` / `guest_vm_linux_password` | No | Linux guest SSH credentials. |
| `guest_vm_win_user` / `guest_vm_win_password` | No | Windows guest WinRM credentials. |

### `type: "hyperv"`

| Field | Required | Notes |
| --- | --- | --- |
| `type` | Yes | Constant `hyperv`. |
| `version` | Yes | Platform version label. |
| `fqdn` | Yes | Hyper-V host address. |
| `api_url` | Yes | Hyper-V host address. |
| `username` | Yes | Hyper-V host login. |
| `password` | Yes | Hyper-V host password. |
| `smb_url` | Yes | SMB share URL used for VM disk transfer. |
| `smb_user` | No | SMB user, when it differs from the provider credentials. |
| `smb_password` | No | SMB password, when it differs from the provider credentials. |
| `guest_vm_linux_user` / `guest_vm_linux_password` | No | Linux guest SSH credentials. |
| `guest_vm_win_user` / `guest_vm_win_password` | No | Windows guest WinRM credentials. |

> **Note:** Hyper-V is the only provider that fetches its CA certificate from port `5986` instead of `443`.

## Fields The Runtime Always Reads

Before dispatching on `type`, the loader builds the provider secret and connection arguments from three fields. They must be present for every provider entry, including
`openshift` and `ova`:

| Field | Use |
| --- | --- |
| `api_url` | Becomes the `url` key of the provider Secret. |
| `username` | Becomes the provider connection username. |
| `password` | Becomes the provider connection password. |

## Guest Credentials

Guest credentials are separate from provider credentials.

The provider `username` and `password` fields log in to the source platform itself. The `guest_vm_*` fields are read later by post-migration SSH or WinRM checks when the
destination VM is powered on. If those checks run and the matching guest credentials are missing, validation fails.

This matters even if the shipped example for a provider does not show guest credentials. The loader keeps extra keys, so it is fine to add `guest_vm_linux_*` and `guest_vm_win_*`
to any provider entry when your selected tests need them.

> **Tip:** Think of `guest_vm_linux_*` and `guest_vm_win_*` as per-guest test credentials, not part of the provider login.

## SSL Behavior

Source-provider SSL behavior is controlled in `tests/tests_config/config.py`, not inside `.providers.json`:

```python
insecure_verify_skip: str = "true"  # SSL verification for OCP API connections
source_provider_insecure_skip_verify: str = "false"  # SSL verification for source provider (VMware, RHV, etc.)
```

Key points:

- `source_provider_insecure_skip_verify` controls the source provider Secret created for vSphere, RHV, OpenStack, and Hyper-V.
- `insecure_verify_skip` is for OpenShift API connections and does not control source provider validation.
- These settings are stored as strings, so use `"true"` or `"false"`.
  When `source_provider_insecure_skip_verify` is `"false"`, the harness fetches a CA certificate from `fqdn` and stores it in the provider Secret for vSphere, OpenStack, and
  Hyper-V.
- RHV is special: the code always fetches the CA certificate, even when verification is skipped, because the ImageIO path still needs it. It is stored under `cacert` by default.
- OpenShift is also special: the source provider reuses the current cluster token Secret, which is created with `insecureSkipVerify: "true"`.
- OVA has no CA download step.

> **Note:** Secure mode only works if `fqdn` points to a host that serves the provider certificate on the expected port. If the certificate fetch fails, provider creation fails.

## vSphere

*Example from `.providers.json.example`:*

```jsonc
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
}
```

What matters for vSphere:

- `fqdn` is used for the direct vSphere connection.
- In secure mode the API URL is normalized for TLS before the certificate is downloaded.
- `vddk_init_image` is passed to the MTV `Provider` resource when present.
- `endpoint_type` maps to `sdkEndpoint` in the MTV `Provider` resource settings. Use `"esxi"` for direct ESXi host connections.
- `luks_passphrase` here is the provider-level fallback. A per-VM `luks_passphrase` in the test plan config takes precedence.

### ESXi cloning via clone_provider

ESXi hosts do not support the `CloneVM_Task` API. That is a vCenter-only operation. To enable VM cloning for test isolation when the source provider is an ESXi host, configure
`clone_provider` so the harness can reach a vCenter entry that performs the clone.

How it works:

- The `clone_provider` value must match a top-level key in `.providers.json`.
- A missing key fails with a `ValueError` that lists the available provider names.
- The referenced entry must be a vSphere provider.
- When `clone_provider` is set, the harness creates a separate `VMWareProvider` connection to that vCenter and uses it for cloning operations.
- If `clone_provider` is not set, the harness uses the source provider for cloning. That works for vCenter entries but will fail for ESXi.

*ESXi provider example:*

```json
"vsphere-esxi": {
  "type": "vsphere",
  "version": "<SERVER VERSION>",
  "fqdn": "ESXI HOST FQDN/IP",
  "api_url": "<ESXI HOST FQDN/IP>/sdk",
  "username": "USERNAME",
  "password": "PASSWORD",
  "endpoint_type": "esxi",
  "vddk_init_image": "<PATH TO VDDK INIT IMAGE>",
  "clone_provider": "vsphere"
}
```

Here `"clone_provider": "vsphere"` references the `"vsphere"` entry shown earlier, which must be a vCenter that manages `ESXI HOST FQDN/IP`.

> **Tip:** Keep a separate vSphere entry for copy-offload, like the example's `vsphere-copy-offload`. That makes it easy to switch between regular and copy-offload runs by
> changing only `source_provider`.

### vSphere copy-offload

The `copyoffload` block is only meaningful for vSphere entries. The code validates it before copy-offload tests run, uses it to build the storage Secret, and passes that Secret
into the `vsphereXcopyConfig` storage-map plugin configuration.

#### Required fields

The schema requires exactly two fields, and the copy-offload fixture re-validates both at runtime:

| Field | Notes |
| --- | --- |
| `storage_vendor_product` | One of the supported vendor values listed below. |
| `datastore_id` | Primary vSphere datastore MoRef ID, such as `datastore-12345`. |

Supported `storage_vendor_product` values:

| Value | Storage array |
| --- | --- |
| `ontap` | NetApp ONTAP |
| `primera3par` | HPE Primera/3PAR |
| `pureFlashArray` | Pure Storage FlashArray |
| `powerflex` | Dell PowerFlex |
| `powermax` | Dell PowerMax |
| `powerstore` | Dell PowerStore |
| `vantara` | Hitachi Vantara |
| `infinibox` | Infinidat InfiniBox |
| `flashsystem` | IBM FlashSystem |

> **Warning:** These values are fixed in code. Use the exact spellings above.

#### Vendor-specific required fields

The schema requires extra fields for some vendors, and the copy-offload fixture raises when a required vendor field is missing:

| `storage_vendor_product` | Additional required fields | Secret keys added |
| --- | --- | --- |
| `ontap` | `ontap_svm` | `ONTAP_SVM` |
| `vantara` | `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list` | `STORAGE_ID`, `STORAGE_PORT`, `HOSTGROUP_ID_LIST` |
| `primera3par` | none | none |
| `pureFlashArray` | `pure_cluster_prefix` | `PURE_CLUSTER_PREFIX` |
| `powerflex` | `powerflex_system_id` | `POWERFLEX_SYSTEM_ID` |
| `powermax` | `powermax_symmetrix_id` | `POWERMAX_SYMMETRIX_ID` |
| `powerstore` | none | none |
| `infinibox` | none | none |
| `flashsystem` | none | none |

#### Optional fields

| Field | Notes |
| --- | --- |
| `default_vm_name` | Overrides the source VM name for cloned copy-offload tests. Applied only to VMs with `clone: true`. |
| `storage_hostname` | Storage array management host. Required at runtime unless `COPYOFFLOAD_STORAGE_HOSTNAME` is set. |
| `storage_username` | Storage array user. Required at runtime unless `COPYOFFLOAD_STORAGE_USERNAME` is set. |
| `storage_password` | Storage array password. Required at runtime unless `COPYOFFLOAD_STORAGE_PASSWORD` is set. |
| `secondary_datastore_id` | Second XCOPY-capable datastore, for multi-datastore tests. |
| `non_xcopy_datastore_id` | Datastore without XCOPY support, for fallback and negative tests. |
| `storage_secret_extra` | Object of extra Kubernetes Secret `stringData` keys for vendor configuration. |
| `dedicated_migration_hosts` | Array of ESXi host MoRef IDs used for XCOPY extraction. Required for dedicated-host tests. |
| `resource_pool` | vSphere resource pool for clone placement. See the priority order below. |
| `esxi_clone_method` | `vib` or `ssh`. `vib` is the default. |
| `esxi_host` / `esxi_user` / `esxi_password` | Required by the schema and at runtime when `esxi_clone_method` is `ssh`. |
| `rdm_lun_uuid` | NAA-format LUN identifier for RDM tests. Must match the `^naa\.` pattern. |

`resource_pool` selection priority order:

1. The configured `resource_pool` value.
2. The target ESXi host's pool, when `target_esxi_host` is set on the clone options.
3. The source VM's or template's pool.
4. A cluster-wide search with a datastore compatibility check.

`dedicated_migration_hosts` must be a non-empty list of non-empty strings. When more than one host is listed, Forklift selects one at random per disk. Dedicated-host tests raise
a `ValueError` when this field is missing.

> **Tip:** Every copy-offload credential can come from an environment variable instead of the file, and environment variables win. Names are built as
> `COPYOFFLOAD_<FIELD_IN_UPPERCASE>`, so examples include `COPYOFFLOAD_STORAGE_HOSTNAME`, `COPYOFFLOAD_STORAGE_USERNAME`, `COPYOFFLOAD_STORAGE_PASSWORD`, `COPYOFFLOAD_ONTAP_SVM`,
> `COPYOFFLOAD_ESXI_HOST`, `COPYOFFLOAD_ESXI_USER`, and `COPYOFFLOAD_ESXI_PASSWORD`. `storage_secret_extra` has its own JSON-object override, `COPYOFFLOAD_STORAGE_SECRET_EXTRA`.

## RHV / oVirt

The RHV source path uses `type: "ovirt"`.

*Example from `.providers.json.example`:*

```jsonc
"ovirt": {
  "type": "ovirt",
  "version": "<SERVER VERSION>",
  "fqdn": "SERVER FQDN/IP",
  "api_url": "<SERVER FQDN/IP>/ovirt-engine/api",
  "username": "USERNAME",
  "password": "PASSWORD"  # pragma: allowlist secret
}
```

What matters for RHV:

- Use `type: "ovirt"` even if you think of the source as RHV.
- `fqdn` should point to the engine host, because the CA certificate is fetched from it.
- The RHV provider also expects a data center named `MTV-CNV` to exist with status `up`. That is not configurable in `.providers.json`, but it is enforced during connection.

> **Note:** RHV is the one provider where the harness always downloads the CA certificate. In secure mode it is used for SDK validation; in insecure mode it is still carried
> because the ImageIO flow needs it.

## OpenStack

*Example from `.providers.json.example`:*

```jsonc
"openstack": {
  "type": "openstack",
  "version": "SERVER VERSION",
  "fqdn": "SERVER FQDN/IP",
  "api_url": "<SERVER FQDN/IP>:<PORT>/v3",
  "username": "USERNAME",
  "password": "PASSWORD",  # pragma: allowlist secret
  "user_domain_name": "<DOMAIN>",
  "region_name": "<REGION>",
  "project_name": "<PROJECT>",
  "user_domain_id": "<USER DOMAIN ID>",
  "project_domain_id": "PROJECT DOMAIN ID",
  "guest_vm_linux_user": "LINUX VMS USERNAME",
  "guest_vm_linux_password": "LINUX VMS PASSWORD"  # pragma: allowlist secret
}
```

What matters for OpenStack:

- `project_name`, `user_domain_name`, `region_name`, `user_domain_id`, and `project_domain_id` are all passed to the provider. All five are required by the schema.
- `fqdn` still matters in secure mode because the harness fetches a CA certificate from it.

## OpenShift

*Example from `.providers.json.example`:*

```jsonc
"openshift": {
  "type": "openshift",
  "version": "<SERVER VERSION>",
  "host": "<CLUSTER API URL>",
  "api_url": "<CLUSTER API URL>",
  "username": "<USERNAME>",
  "password": "<PASSWORD>",  # pragma: allowlist secret
  "storage_class": "<STORAGE CLASS NAME>"
}
```

What matters for OpenShift:

- Keep the placeholder shape from the example; `openshift` entries allow no extra keys.
- The OpenShift source provider does not use these values to log in. The code rewrites `api_url` to the current cluster host and reuses the current cluster token Secret.
- Because this provider block sets `additionalProperties: false`, do not add guest credential keys here. Use a separate source provider entry if your tests need them.
- The schema requires `host` and `storage_class`, but the loader never reads them. Target storage comes from the runtime `storage_class` value passed with `--tc`.

> **Note:** The reused OpenShift Secret is created with `insecureSkipVerify: "true"`, so `source_provider_insecure_skip_verify` does not affect OpenShift the same way it
> affects vSphere, RHV, OpenStack, or Hyper-V.

## OVA

*Example from `.providers.json.example`:*

```jsonc
"ova": {
  "type": "ova",
  "version": "<SERVER VERSION>", # Can be anything, just placeholder
  "fqdn": "",
  "api_url": "<NFS SHARE URL>",
  "username": "<USERNAME>",
  "password": ""  # pragma: allowlist secret
}
```

What matters for OVA:

- `api_url` is the NFS share URL and the only field the OVA provider consumes for its connection.
- `version` is used for naming, not protocol negotiation.
- `fqdn`, `username`, and `password` must still be present because the loader reads them for every provider type.
- OVA source VMs are never cloned. The test flow requires each plan VM name to match an existing OVA file.
- There is no CA download step for OVA.

## Hyper-V

*Example from `.providers.json.example`:*

```jsonc
"hyperv": {
  "type": "hyperv",
  "version": "2025",
  "fqdn": "hyperv-host.example.com",
  "api_url": "hyperv-host.example.com",
  "username": "Administrator",
  "password": "PASSWORD",  # pragma: allowlist secret
  "smb_url": "//hyperv-host.example.com/VMShare",
  "smb_user": "",  # Optional: SMB username if different from provider credentials
  "smb_password": "",  # pragma: allowlist secret  # Optional: SMB password if different from provider credentials
  "guest_vm_linux_user": "root",
  "guest_vm_linux_password": "PASSWORD",  # pragma: allowlist secret
  "guest_vm_win_user": "Administrator",
  "guest_vm_win_password": "PASSWORD"  # pragma: allowlist secret
}
```

What matters for Hyper-V:

- `smb_url` is required by the schema and is what the provider uses to transfer VM disks.
- `smb_user` and `smb_password` are optional. When present and non-empty they become the `smbUser` and `smbPassword` Secret keys.
- In secure mode the CA certificate is fetched from `fqdn` on port `5986`.
- Hyper-V guest OS detection can be overridden per VM with the `win_os` key in the test plan config.

## Practical Checklist

- Start from `.providers.json.example`, then remove all comments before saving the real `.providers.json`.
- Validate your editor against the `providers_schema.json` URL in the `$schema` key.
- Make sure your `source_provider` setting matches a top-level key in the file.
- Keep `fqdn` accurate for vSphere, RHV, OpenStack, and Hyper-V, especially if SSL verification is enabled.
- Add guest credentials for any provider entry whose powered-on guests will be validated over SSH or WinRM.
- For vSphere copy-offload, populate both the common storage credentials and the vendor-specific fields required by your chosen `storage_vendor_product`.
- Remember that OpenShift is the only provider type that rejects unknown keys.
