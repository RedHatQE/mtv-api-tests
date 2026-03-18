# Provider Config File

`mtv-api-tests` does not ship a separate JSON Schema document for provider settings. The effective schema comes from `.providers.json.example` and the Python code that loads `./.providers.json`. In practice, that means the file is flexible, but specific fields become required when a provider path or validation step reads them.

> **Warning:** `.providers.json.example` is an annotated template, not a ready-to-use `.providers.json`. The loader parses `.providers.json` with `json.loads(...)`, so your real file must be strict JSON: remove comments, keep valid quoting, and avoid trailing commas.

> **Warning:** `.providers.json` usually contains provider passwords and guest OS passwords. Treat it as a secret file.

> **Note:** The top-level key is the provider name you select with `source_provider`. It does not have to match `type`. For example, the example file has a key named `vsphere-copy-offload`, but its `"type"` is still `"vsphere"`.

## How the file is used

- The test harness looks for `./.providers.json` in the directory where you run the tests.
- The file can contain multiple provider entries in one JSON object.
- `source_provider` must match one of the top-level keys in that file.
- A missing or empty file fails fast.
- `version` is used mainly in generated test resource names. It is not the field that decides how to connect.
- There is no strict field whitelist. Extra keys are usually harmless until a specific provider or test path reads them.

## Common fields

| Field | Meaning | Notes |
| --- | --- | --- |
| `type` | Provider implementation to use | Supported values from the example file are `vsphere`, `ovirt`, `openstack`, `openshift`, and `ova`. |
| `version` | Provider version label | Used mainly in generated test resource names. Keep it populated for every entry. |
| `fqdn` | Provider host name or IP | Important for VMware direct connections and for CA certificate download in secure VMware, RHV, and OpenStack flows. |
| `api_url` | Provider API endpoint or share URL | Expected format depends on the provider: `/sdk` for vSphere, `/ovirt-engine/api` for RHV, `/v3` for OpenStack, and an NFS share URL for OVA. |
| `username` / `password` | Provider login credentials | Required for VMware, RHV, and OpenStack. Kept as placeholders in the OpenShift and OVA examples. |
| `guest_vm_linux_user` / `guest_vm_linux_password` | Linux guest login | Used for SSH-based post-migration validation, not for connecting to the source provider. |
| `guest_vm_win_user` / `guest_vm_win_password` | Windows guest login | Also used for post-migration validation, not provider login. |
| `vddk_init_image` | vSphere-specific provider field | Passed through to the MTV `Provider` resource when set. |
| `copyoffload` | vSphere-only nested settings | Used by copy-offload tests to build storage secrets and storage-map plugin config. |

## Guest credentials

Guest credentials are separate from provider credentials.

The provider `username` and `password` fields log in to the source platform itself. The `guest_vm_*` fields are read later by post-migration SSH checks when the destination VM is powered on. If those checks run and the matching guest credentials are missing, validation fails.

This matters even if the shipped example for a provider does not show guest credentials. The loader keeps extra keys, so it is fine to add `guest_vm_linux_*` and `guest_vm_win_*` to any provider entry when your selected tests need them.

> **Tip:** Think of `guest_vm_linux_*` and `guest_vm_win_*` as per-guest test credentials, not part of the provider login.

## SSL behavior

Source-provider SSL behavior is controlled in `tests/tests_config/config.py`, not inside `.providers.json`:

```python
insecure_verify_skip: str = "true"  # SSL verification for OCP API connections
source_provider_insecure_skip_verify: str = "false"  # SSL verification for source provider (VMware, RHV, etc.)
```

Key points:

- `source_provider_insecure_skip_verify` controls the source provider secret created for VMware, RHV, OpenStack, and OVA.
- `insecure_verify_skip` is for OpenShift API connections and does not control VMware/RHV/OpenStack provider validation.
- These settings are stored as strings, so use `"true"` or `"false"`.
- When `source_provider_insecure_skip_verify` is `"false"`, the harness fetches a CA certificate from `fqdn:443` and stores it in the provider secret for VMware and OpenStack.
- RHV is special: the code always fetches the CA certificate, even when verification is skipped, because the ImageIO path still needs it.
- OpenShift is also special: the source provider reuses the current cluster token secret, which is created with `insecureSkipVerify: "true"`.
- OVA has no CA download step.

> **Note:** Secure mode only works if `fqdn` points to a host that serves the provider certificate on port `443`. If the certificate fetch fails, provider creation fails.

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
  "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
}
```

What matters for vSphere:

- `type` must be `vsphere`.
- `fqdn` is used for the direct vSphere connection.
- `api_url` becomes the MTV provider URL and should end with `/sdk`.
- `username` and `password` are the vSphere credentials used by the harness.
- The Linux and Windows guest credentials are used only for guest-level validation after migration.
- `vddk_init_image` is passed to the MTV `Provider` resource when present.

> **Tip:** Keep a separate vSphere entry for copy-offload, like the example’s `vsphere-copy-offload`. That makes it easy to switch between regular and copy-offload test runs by changing only `source_provider`.

## vSphere copy-offload

The `copyoffload` section is only meaningful for vSphere entries. The code validates it before copy-offload tests run, uses it to build the storage secret, and then passes that secret into the `vsphereXcopyConfig` storage-map plugin configuration.

*Core copy-offload fields from `.providers.json.example`:*

```jsonc
"copyoffload": {
  # Supported storage_vendor_product values:
  # - "ontap"           (NetApp ONTAP)
  # - "vantara"         (Hitachi Vantara)
  # - "primera3par"     (HPE Primera/3PAR)
  # - "pureFlashArray"  (Pure Storage FlashArray)
  # - "powerflex"       (Dell PowerFlex)
  # - "powermax"        (Dell PowerMax)
  # - "powerstore"      (Dell PowerStore)
  # - "infinibox"       (Infinidat InfiniBox)
  # - "flashsystem"     (IBM FlashSystem)
  "storage_vendor_product": "ontap",

  # Primary datastore for copy-offload operations (required)
  # This is the vSphere datastore ID (e.g., "datastore-12345") where VMs reside
  # Get via vSphere: Datacenter → Storage → Datastore → Summary → More Objects ID
  "datastore_id": "datastore-12345",

  # Optional: Secondary datastore for multi-datastore copy-offload tests
  # Only needed when testing VMs with disks spanning multiple datastores
  # When specified, tests can validate copy-offload with disks on different datastores
  "secondary_datastore_id": "datastore-67890",

  # Optional: Non-XCOPY datastore for mixed datastore tests
  # This should be a datastore that does NOT support XCOPY/VAAI primitives
  # Used for testing VMs with disks on both XCOPY and non-XCOPY datastores
  "non_xcopy_datastore_id": "datastore-99999",

  "default_vm_name": "rhel9-template",
  "storage_hostname": "storage.example.com",
  "storage_username": "admin",
  "storage_password": "your-password-here",  # pragma: allowlist secret
```

Copy-offload field reference:

| Field | Required when | Meaning |
| --- | --- | --- |
| `storage_vendor_product` | Always | Storage backend name. Must be one of the supported values listed above. |
| `datastore_id` | Always | Primary vSphere datastore MoRef ID, such as `datastore-12345`. |
| `storage_hostname` | Always, unless provided by environment variable | Storage system host used to build the copy-offload secret. |
| `storage_username` | Always, unless provided by environment variable | Storage login name. |
| `storage_password` | Always, unless provided by environment variable | Storage password. |
| `secondary_datastore_id` | Only for multi-datastore tests | Second XCOPY-capable datastore. |
| `non_xcopy_datastore_id` | Only for mixed/fallback tests | Datastore that does not support XCOPY/VAAI. |
| `default_vm_name` | Optional | Overrides the source VM/template name for cloned copy-offload tests. |
| `esxi_clone_method` | Optional | `vib` is the default. Set it to `ssh` to make the provider use SSH-based ESXi cloning. |
| `esxi_host` / `esxi_user` / `esxi_password` | Required when `esxi_clone_method` is `ssh` | ESXi SSH connection settings. |
| `rdm_lun_uuid` | Only for RDM tests | Required when running RDM disk tests. |

Vendor-specific fields:

| `storage_vendor_product` value | Additional fields |
| --- | --- |
| `ontap` | `ontap_svm` |
| `vantara` | `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list` |
| `primera3par` | none |
| `pureFlashArray` | `pure_cluster_prefix` |
| `powerflex` | `powerflex_system_id` |
| `powermax` | `powermax_symmetrix_id` |
| `powerstore` | none |
| `infinibox` | none |
| `flashsystem` | none |

> **Tip:** Every copy-offload credential can come from an environment variable instead of the file, and environment variables win. The code builds names as `COPYOFFLOAD_<FIELD_IN_UPPERCASE>`, so examples include `COPYOFFLOAD_STORAGE_HOSTNAME`, `COPYOFFLOAD_STORAGE_USERNAME`, `COPYOFFLOAD_STORAGE_PASSWORD`, `COPYOFFLOAD_ONTAP_SVM`, `COPYOFFLOAD_ESXI_HOST`, `COPYOFFLOAD_ESXI_USER`, and `COPYOFFLOAD_ESXI_PASSWORD`.

> **Warning:** The supported `storage_vendor_product` values are fixed in code. Use the exact spellings shown in the example and table above.

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
- `api_url` should point to the engine API and end with `/ovirt-engine/api`.
- `fqdn` should point to the engine host, because the CA certificate is fetched from `fqdn:443`.
- `username` and `password` are required for the provider connection.
- If your selected tests perform SSH-based guest validation, add `guest_vm_linux_*` and `guest_vm_win_*` to this entry even though the example does not show them.

> **Note:** RHV is the one provider where the harness always downloads the CA certificate. In secure mode it is used for SDK validation; in insecure mode it is still carried because the ImageIO flow needs it.

> **Note:** The RHV provider code also expects a data center named `MTV-CNV` to exist and be `up`. That is not configured in `.providers.json`, but it is enforced during connection.

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

- `api_url` should be the Keystone v3 endpoint.
- `project_name`, `user_domain_name`, `region_name`, `user_domain_id`, and `project_domain_id` are all read by the OpenStack provider code. Keep all of them populated.
- `fqdn` still matters in secure mode because the harness fetches a CA certificate from `fqdn:443`.
- The example includes Linux guest credentials because post-migration validation may SSH into powered-on Linux guests.
- If your test selection includes powered-on Windows guests with guest-level validation, add `guest_vm_win_user` and `guest_vm_win_password` as well.

## OpenShift

*Example from `.providers.json.example`:*

```jsonc
"openshift": {
  "type": "openshift",
  "version": "<SERVER VERSION>",
  "fqdn": "",
  "api_url": "",
  "username": "",
  "password": ""  # pragma: allowlist secret
}
```

What matters for OpenShift:

- Keep the placeholder shape from the example.
- In this repo, the OpenShift source provider does not use `fqdn`, `api_url`, `username`, or `password` from `.providers.json` to log in.
- Instead, the code rewrites the URL to the current cluster and reuses the current cluster token secret.
- The blank values in the example are intentional.
- If you run OpenShift-source scenarios that perform guest SSH validation, you can still add `guest_vm_linux_*` and `guest_vm_win_*` to this entry even though the example omits them.

> **Note:** The reused OpenShift secret is created with `insecureSkipVerify: "true"`, so `source_provider_insecure_skip_verify` does not affect OpenShift the same way it affects VMware, RHV, or OpenStack.

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

- `api_url` is the NFS share URL.
- The example already notes that `version` can be a placeholder. The code mainly uses it for naming, not for protocol negotiation.
- The current OVA provider implementation only consumes `api_url`.
- `username` and `password` stay in the example mostly to keep the provider entry shape consistent.
- The OVA test path uses a fixed source VM name, `1nisim-rhel9-efi`, rather than selecting a source VM name from `.providers.json`.
- There is no CA download step for OVA.

## Practical checklist

- Start from `.providers.json.example`, then remove all comments before saving the real `.providers.json`.
- Make sure your `source_provider` setting matches a top-level key in the file.
- Keep `fqdn` accurate for VMware, RHV, and OpenStack, especially if SSL verification is enabled.
- Add guest credentials for any provider entry whose powered-on guests will be validated over SSH.
- For vSphere copy-offload, populate both the common storage credentials and the vendor-specific fields required by your chosen `storage_vendor_product`.
