# Toehold (Copy Appliance) disk transfer: MTV deploys a copy appliance VM inside vSphere and
# exposes the disks through it over NBD instead of using VDDK (VIRTSTRAT-757, MTV 5.0).

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.copy_appliance_template import CopyApplianceTemplate
from ocp_resources.deployment import Deployment
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from ocp_resources.role_binding import RoleBinding
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler, TimeoutExpiredError

from utilities.copyoffload_constants import FORKLIFT_CONTROLLER_NAME
from utilities.naming import K8S_NAME_MAX_LENGTH
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

CNV_NAMESPACE = "openshift-cnv"
CDI_CONTROLLER_DEPLOYMENT = "cdi-deployment"

# The product's copy appliance gate (settings.CopyAppliance.EnabledForPlan) returns false when
# a VDDK image is configured, and settings.GetVDDKImage falls back to the controller's global
# VDDK_IMAGE env var when the provider setting is empty. Both must be clear for the appliance
# to be used, so the toehold setup asserts on them.
VDDK_INIT_IMAGE_SETTING = "vddkInitImage"
GLOBAL_VDDK_IMAGE_ENV = "VDDK_IMAGE"

# The forklift operator rolls the controller deployment when its CR changes; used for
# waiting until the controller reports RUNNING again after the copy appliance patch.
FORKLIFT_CONTROLLER_ROLLOUT_TIMEOUT = 600

# CDI image override: patching the CNV CSV env makes OLM roll the CDI deployments, and the
# cdi operator then redeploys the controller with the overridden image.
CDI_ROLLOUT_TIMEOUT = 600

# Template build (libguestfs customize, image upload, vSphere clone) takes minutes; the
# provider's appliance check pod runs only after the template is ready.
COPY_APPLIANCE_TEMPLATE_TIMEOUT = 1800
COPY_APPLIANCE_CHECK_TIMEOUT = 1800

# The provider controller generates the copy appliance SSH secrets asynchronously after the
# settings patch; the CAT controller fails terminally if it runs before they exist.
PROVIDER_COPY_APPLIANCE_SECRETS_TIMEOUT = 300

# Hardcoded toehold (copy appliance) configuration, keyed by source provider name. These
# values are specific to the dev-preview feature build (VIRTSTRAT-757) and the QE vCenter
# inventory — the appliance placement (resource pool, datastore, folder, network) belongs to
# a specific vCenter, not to a vSphere version, so keying by provider name keeps an
# unrelated vCenter running the same vSphere version from receiving these paths. A provider
# absent here has no appliance configuration: --toehold fails fast for it, and without the
# flag the regular VDDK transfer path runs unchanged.
_TOEHOLD_SHARED_CONFIG = {
    "folder": "/Datacenter/vm",
    "network": "VM Network",
    # Pinned by digest: the appliance template build is not reproducible with a floating tag.
    "base_disk_container_image": "registry.redhat.io/rhel9/rhel-guest-image@sha256:18019682bc8c7ca8e682a86d3df473eceae2543f32b319bd15aa20f83deeff12",
    "cdi_controller_image": "quay.io/mnecas0/cdi-controller@sha256:bea85a65324f91025d7e6e99e7b86558e119f5e0b3d098435c8d4daa0d0e9f16",
    "cdi_importer_image": "quay.io/mnecas0/cdi-importer@sha256:5501157e5d18bfe6b696686b730bc4f57c56160874659ede1de51441e8418008",
}

TOEHOLD_CONFIG_BY_PROVIDER = {
    "vsphere-7.0.3": {
        **_TOEHOLD_SHARED_CONFIG,
        "resource_pool": "/Datacenter/host/10.6.46.28/Resources",
        # Place the appliance template and every appliance clone on the large VMFS, not the
        # 94.8 GiB datastore1: a tier0 run needs the template plus one clone per migrating VM
        # (and the shared-disk class clones 2x16 GiB of source VMs onto datastore1).
        "datastore": "datastore2",
    },
    "vsphere-8.0.1": {
        **_TOEHOLD_SHARED_CONFIG,
        "resource_pool": "/Datacenter/host/mtv-cluster/Resources",
        # This vCenter has no large VMFS: datastore1 is 94.8 GiB, so the appliance goes on the
        # NFS datastore the tests already use.
        "datastore": "mtv-nfs-psi-rdu2-v8",
    },
}


def toehold_usage_error(
    provider_name: str,
    provider_type: str | None,
    provider_data: dict[str, Any],
) -> str | None:
    """Return the --toehold usage error for the selected source provider.

    Args:
        provider_name (str): Selected source provider name from the test config.
        provider_type (str | None): Provider type (None when the provider is not configured).
        provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        str | None: The usage error message, or None when the provider supports toehold.
    """
    if provider_type != Provider.ProviderType.VSPHERE or provider_name not in TOEHOLD_CONFIG_BY_PROVIDER:
        return (
            f"--toehold has no copy appliance configuration for source provider "
            f"'{provider_name}' (type '{provider_type}', version "
            f"'{provider_data.get('version', 'unknown')}'). Supported providers: "
            f"{', '.join(TOEHOLD_CONFIG_BY_PROVIDER)}."
        )
    return None


def get_toehold_config(provider_name: str, provider_type: str) -> dict[str, Any]:
    """Return the copy appliance configuration for the source provider.

    Args:
        provider_name (str): Source provider name.
        provider_type (str): Source provider type.

    Returns:
        dict[str, Any]: The toehold configuration for the provider.

    Raises:
        ValueError: If the provider type is not vSphere or the provider has no appliance
            configuration.
    """
    if provider_type != Provider.ProviderType.VSPHERE:
        raise ValueError(f"Toehold requires a vSphere provider, but got '{provider_type}'.")
    if provider_name not in TOEHOLD_CONFIG_BY_PROVIDER:
        raise ValueError(
            f"No copy appliance configuration for source provider '{provider_name}'. "
            f"Supported providers: {', '.join(TOEHOLD_CONFIG_BY_PROVIDER)}."
        )
    return TOEHOLD_CONFIG_BY_PROVIDER[provider_name]


def restore_forklift_controller(
    editor: ResourceEditor,
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
) -> None:
    """Restore the controller template config and wait until the controller is Running again.

    Restoring the CR only requests the change; the operator reconciles asynchronously, so
    without this wait the session can end while the controller still serves the toehold
    template settings and a following regular run picks them up.

    Args:
        editor (ResourceEditor): The editor holding the controller template-config backup.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the ForkliftController is installed.

    Raises:
        TimeoutExpiredError: If the controller does not report Running in time.
    """
    editor.restore()
    _wait_for_forklift_controller_running(
        forklift_controller=ForkliftController(
            client=ocp_admin_client,
            name=FORKLIFT_CONTROLLER_NAME,
            namespace=mtv_namespace,
            ensure_exists=True,
        )
    )


def _wait_for_forklift_controller_running(forklift_controller: ForkliftController) -> None:
    """Wait until the ForkliftController reports its Running condition.

    The operator reconciles the CR asynchronously, so both applying and restoring the template
    config need this wait: returning before the controller is Running again would let a later
    regular run start against a controller that still serves the previous configuration.

    Args:
        forklift_controller (ForkliftController): The ForkliftController to wait for.

    Raises:
        TimeoutExpiredError: If the controller does not report Running in time.
    """
    forklift_controller.wait_for_condition(
        status=forklift_controller.Condition.Status.TRUE,
        condition=forklift_controller.Condition.Type.RUNNING,
        timeout=FORKLIFT_CONTROLLER_ROLLOUT_TIMEOUT,
    )


def patch_forklift_controller(
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
    toehold_config: dict[str, Any],
) -> ResourceEditor:
    """Enable the copy appliance feature and its template config on the ForkliftController.

    The MTV 5.0 GA controller already implements the copy appliance pipeline, so only the
    feature gate and the template config are patched here; the CDI image override (the
    appliance import path is not in GA CDI) is applied separately. With
    feature_copy_appliance enabled, the provider controller also maintains the provider's
    CopyApplianceTemplate from the copy_appliance_template_* fields and the provider's
    copyAppliance settings.

    The feature gate itself is intentionally NOT restored on teardown: it is inert without a
    provider CopyApplianceTemplate (the appliance check and the plan skip the path when no
    template exists), and test clusters keep it enabled between runs. Only the template
    config overrides are restored by the returned editor.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the ForkliftController is installed.
        toehold_config (dict[str, Any]): Validated toehold configuration.

    Returns:
        ResourceEditor: The editor holding the backup of the template config overrides to
            restore on teardown.
    """
    forklift_controller = ForkliftController(
        client=ocp_admin_client,
        name=FORKLIFT_CONTROLLER_NAME,
        namespace=mtv_namespace,
        ensure_exists=True,
    )

    # No backup: the gate stays enabled after the run.
    ResourceEditor(patches={forklift_controller: {"spec": {"feature_copy_appliance": "true"}}}).update()

    patches: dict[str, str] = {
        "copy_appliance_template_base_disk_container_image_fqin": toehold_config["base_disk_container_image"],
    }
    forklift_controller = ForkliftController(
        client=ocp_admin_client,
        name=FORKLIFT_CONTROLLER_NAME,
        namespace=mtv_namespace,
        ensure_exists=True,
    )
    LOGGER.info(f"Patching the ForkliftController copy appliance config: {sorted(patches)}")
    editor = ResourceEditor(patches={forklift_controller: {"spec": patches}})
    editor.update(backup_resources=True)

    rolled_out = False
    try:
        _wait_for_forklift_controller_running(forklift_controller=forklift_controller)
        rolled_out = True
    finally:
        # A cluster-wide controller override must not survive a failed rollout — restore on
        # ANY failure (timeout or an API error mid-rollout), matching the CDI override fix.
        if not rolled_out:
            editor.restore()
    return editor


def patch_provider_copy_appliance_settings(
    provider: Provider,
    toehold_config: dict[str, Any],
) -> ResourceEditor:
    """Patch the provider's copy appliance placement settings, with backup for restore.

    The provider controller's template sync fills the CopyApplianceTemplate spec from the
    provider's copyAppliance settings and the controller's template config, so the settings
    must be in place before the feature is enabled on the controller.

    The provider's vddkInitImage is cleared in the same patch: the product gives VDDK
    priority over the copy appliance, so otherwise the appliance is validated but never used.

    Args:
        provider (Provider): The source Provider OCP resource.
        toehold_config (dict[str, Any]): Validated toehold configuration.

    Returns:
        ResourceEditor: Editor applied with backup_resources=True; call restore() on teardown.
    """
    return ResourceEditor(
        patches={
            provider: {
                "spec": {
                    "settings": {
                        # A merge-patch null deletes the key. The product gives VDDK priority
                        # over the copy appliance (EnabledForPlan), so a provider that keeps
                        # vddkInitImage silently transfers every disk over VDDK even when the
                        # appliance is built and validated.
                        "vddkInitImage": None,
                        "copyApplianceDatastore": toehold_config["datastore"],
                        "copyApplianceFolder": toehold_config["folder"],
                        "copyApplianceNetwork": toehold_config["network"],
                        "copyApplianceResourcePool": toehold_config["resource_pool"],
                    }
                }
            }
        }
    )


def assert_copy_appliance_path_enabled(
    provider: Provider,
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
) -> None:
    """Fail fast when the product would still prefer VDDK over the copy appliance.

    The appliance is built and validated regardless of the VDDK setting, so a fallback is
    silent: every disk transfers over VDDK while the run still looks like a toehold run.
    This mirrors the product gate before the migration, including its fallback to the
    controller's global VDDK image.

    Args:
        provider (Provider): The source Provider OCP resource.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the ForkliftController is installed.

    Raises:
        ValueError: If a VDDK image would take priority over the copy appliance.
    """
    # Re-read: ResourceEditor patches through the API without refreshing the wrapper's
    # cached instance, so the in-memory provider would still show the cleared setting.
    patched_provider = Provider(
        client=ocp_admin_client,
        name=provider.name,
        namespace=provider.namespace,
        ensure_exists=True,
    )
    provider_settings = patched_provider.instance.to_dict()["spec"].get("settings") or {}
    if provider_vddk_image := provider_settings.get(VDDK_INIT_IMAGE_SETTING):
        raise ValueError(
            f"Provider '{provider.name}' sets {VDDK_INIT_IMAGE_SETTING} "
            f"('{provider_vddk_image}'). The product prefers VDDK over the copy appliance, "
            "so --toehold would transfer every disk over VDDK instead of the appliance."
        )

    controller_deployment = Deployment(
        client=ocp_admin_client,
        name=FORKLIFT_CONTROLLER_NAME,
        namespace=mtv_namespace,
        ensure_exists=True,
    )
    controller_dict = controller_deployment.instance.to_dict()
    for container in controller_dict["spec"]["template"]["spec"]["containers"]:
        for env_var in container.get("env", []):
            if env_var["name"] == GLOBAL_VDDK_IMAGE_ENV and env_var.get("value"):
                raise ValueError(
                    f"Forklift controller deployment sets a global {GLOBAL_VDDK_IMAGE_ENV} "
                    f"('{env_var['value']}'). The product falls back to it when the provider "
                    "has no VDDK image, so --toehold would transfer every disk over VDDK."
                )


def copy_appliance_template_name(provider: Provider) -> str:
    """Return the CopyApplianceTemplate name the controllers look up for this provider.

    Args:
        provider (Provider): The source Provider OCP resource.

    Returns:
        str: ``<provider>-copy-appliance-template``.

    Raises:
        ValueError: If the name exceeds the Kubernetes name limit — truncation would break the
            controller lookup, so an over-long provider name is a configuration error.
    """
    template_name = f"{provider.name}-copy-appliance-template"
    if len(template_name) > K8S_NAME_MAX_LENGTH:
        raise ValueError(
            f"CopyApplianceTemplate name '{template_name}' exceeds the Kubernetes limit "
            f"({K8S_NAME_MAX_LENGTH} chars); use a shorter provider name."
        )
    return template_name


def assert_copy_appliance_template_absent(
    ocp_admin_client: DynamicClient,
    provider: Provider,
    template_name: str,
) -> None:
    """Fail fast when a template with the provider's fixed name already exists.

    The template name is fixed per provider, so an existing one is a leftover from another
    session: reusing (and deleting it on teardown) would destroy foreign state.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        provider (Provider): The source Provider OCP resource.
        template_name (str): The fixed template name for that provider.

    Raises:
        ValueError: If a CopyApplianceTemplate with that name already exists.
    """
    for existing_template in CopyApplianceTemplate.get(client=ocp_admin_client, namespace=provider.namespace):
        if existing_template.name == template_name:
            raise ValueError(
                f"CopyApplianceTemplate '{template_name}' already exists in namespace "
                f"'{provider.namespace}'; delete the leftover before running."
            )


def create_provider_copy_appliance_template(
    ocp_admin_client: DynamicClient,
    toehold_config: dict[str, Any],
    fixture_store: dict[str, Any],
    provider: Provider,
) -> CopyApplianceTemplate:
    """Create the provider's CopyApplianceTemplate once its SSH secrets exist.

    The template is created by the API (this suite), not the operator, but its name is fixed:
    the provider controller's appliance check, the plan controller and the template sync all
    look it up as <provider>-copy-appliance-template in the provider's namespace.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        toehold_config (dict[str, Any]): Validated toehold configuration.
        fixture_store (dict[str, Any]): Session fixture store for resource tracking.
        provider (Provider): The source Provider OCP resource.

    Returns:
        CopyApplianceTemplate: The created CopyApplianceTemplate resource.

    Raises:
        TimeoutExpiredError: If the provider does not name its copy appliance SSH secrets in
            time.
        ValueError: If the template name exceeds the Kubernetes name limit or a template with
            that name already exists in the provider namespace.
    """
    _wait_for_provider_copy_appliance_secrets(provider=provider)

    template_name = copy_appliance_template_name(provider=provider)
    assert_copy_appliance_template_absent(
        ocp_admin_client=ocp_admin_client,
        provider=provider,
        template_name=template_name,
    )

    return create_and_store_resource(
        fixture_store=fixture_store,
        resource=CopyApplianceTemplate,
        client=ocp_admin_client,
        name=template_name,
        namespace=provider.namespace,
        provider={"name": provider.name, "namespace": provider.namespace},
        base_disk={"containerImage": toehold_config["base_disk_container_image"]},
        datastore=toehold_config["datastore"],
        folder=toehold_config["folder"],
        network=toehold_config["network"],
        template_name=template_name,
        # The caller owns the fixed name; the pre-check above covers the common case and this
        # covers the race with the provider controller creating it concurrently.
        on_conflict="fail",
    )


def wait_copy_appliance_template_ready(
    copy_appliance_template: CopyApplianceTemplate,
    provider: Provider,
) -> None:
    """Wait until the template is built and the provider's appliance check passes.

    Fails fast on a terminal Failed build phase instead of blocking for the full
    timeout, so the controller's failure details surface immediately.

    Args:
        copy_appliance_template (CopyApplianceTemplate): The template to wait for.
        provider (Provider): The source Provider OCP resource.

    Raises:
        TimeoutExpiredError: If the template build or the provider appliance check does not
            succeed in time.
        ValueError: If the template build or the provider appliance check fails.
    """
    LOGGER.info(f"Waiting for CopyApplianceTemplate {copy_appliance_template.name} to build")
    for phase in TimeoutSampler(
        wait_timeout=COPY_APPLIANCE_TEMPLATE_TIMEOUT,
        sleep=10,
        func=lambda: getattr(getattr(copy_appliance_template.instance, "status", None), "phase", None),
    ):
        if phase == "Succeeded":
            break
        if phase == "Failed":
            raise ValueError(
                f"Copy appliance template '{copy_appliance_template.name}' build failed: "
                f"{copy_appliance_template.instance.status}"
            )

    _wait_for_provider_copy_appliance_check(provider=provider)


def create_copy_appliance_nbd_puller_rolebinding(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    mtv_namespace: str,
    target_namespace: str,
) -> RoleBinding:
    """Grant the automation namespace's default ServiceAccount pull access to the nbd image.

    Temporary workaround for MTV-7457: the copy appliance setup pod runs as the default
    ServiceAccount of the automation namespace and pulls the operator-built nbd-container
    image from the internal registry imagestream in the MTV namespace. Without this
    RoleBinding the pull is denied and the provider's appliance check fails with "setting
    the appliance up failed". Remove this once the fix ships in an MTV build.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Session fixture store for resource tracking.
        mtv_namespace (str): Namespace where the nbd imagestream lives (openshift-mtv).
        target_namespace (str): Automation namespace whose default ServiceAccount pulls.

    Returns:
        RoleBinding: The created RoleBinding resource.
    """
    return create_and_store_resource(
        fixture_store=fixture_store,
        resource=RoleBinding,
        client=ocp_admin_client,
        name=f"copy-appliance-nbd-puller-{target_namespace}",
        namespace=mtv_namespace,
        role_ref_kind="ClusterRole",
        role_ref_name="system:image-puller",
        subjects_kind="ServiceAccount",
        subjects_name="default",
        subjects_namespace=target_namespace,
    )


class CdiImageOverride:
    """A CNV CSV CDI image override with its backup and a rollback wait.

    Restoring the CSV only requests the rollback: OLM rolls the CDI deployments
    asynchronously, so a restore must also wait until the original controller image is
    serving again before the cluster is safe for regular (non-toehold) runs.
    """

    def __init__(self, editor: ResourceEditor, original_controller_image: str) -> None:
        """Store the backup editor and the pre-patch CDI controller image.

        Args:
            editor (ResourceEditor): Editor holding the CSV backup (update applied with
                backup_resources=True).
            original_controller_image (str): The CDI controller deployment image before
                the override.
        """
        self.editor = editor
        self.original_controller_image = original_controller_image

    def restore(self, ocp_admin_client: DynamicClient) -> None:
        """Restore the CSV and wait until the original CDI controller is serving again.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.

        Raises:
            TimeoutExpiredError: If the CDI deployment does not roll back in time.
        """
        self.editor.restore()
        _wait_for_cdi_rollout(
            ocp_admin_client=ocp_admin_client,
            controller_image=self.original_controller_image,
        )


def patch_cdi_images(
    ocp_admin_client: DynamicClient,
    toehold_config: dict[str, Any],
) -> CdiImageOverride:
    """Override the CDI controller and importer images on the CNV CSV.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        toehold_config (dict[str, Any]): Validated toehold configuration.

    Returns:
        CdiImageOverride: The override holding the backup and rollback wait.

    Raises:
        ValueError: If no CNV CSV with a CDI deployment is found.
    """
    controller_image = toehold_config["cdi_controller_image"]
    importer_image = toehold_config["cdi_importer_image"]

    cdi_csv = _get_cdi_csv(ocp_admin_client=ocp_admin_client)
    cdi_deployment = _get_cdi_controller_deployment(ocp_admin_client=ocp_admin_client)
    original_controller_image = str(cdi_deployment.instance.spec.template.spec.containers[0].image)
    patched_deployments = _patched_cdi_csv_deployments(
        csv_dict=cdi_csv.instance.to_dict(),
        controller_image=controller_image,
        importer_image=importer_image,
    )

    LOGGER.info(f"Patching the CDI images on {cdi_csv.name} (controller: {controller_image})")
    editor = ResourceEditor(patches={cdi_csv: {"spec": {"install": {"spec": {"deployments": patched_deployments}}}}})
    editor.update(backup_resources=True)
    cdi_override = CdiImageOverride(editor=editor, original_controller_image=original_controller_image)
    rolled_out = False
    try:
        _wait_for_cdi_rollout(
            ocp_admin_client=ocp_admin_client,
            controller_image=controller_image,
        )
        rolled_out = True
    finally:
        # A cluster-wide CDI override must not survive a failed rollout — restore on ANY
        # failure (timeout or an API error mid-rollout), not just TimeoutExpiredError.
        if not rolled_out:
            cdi_override.restore(ocp_admin_client=ocp_admin_client)
    return cdi_override


def _get_cdi_controller_deployment(ocp_admin_client: DynamicClient) -> Deployment:
    """Return the CDI controller deployment handle shared by the patch and rollout helpers.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.

    Returns:
        Deployment: The CDI controller deployment.
    """
    return Deployment(
        client=ocp_admin_client,
        name=CDI_CONTROLLER_DEPLOYMENT,
        namespace=CNV_NAMESPACE,
        ensure_exists=True,
    )


def _get_cdi_csv(ocp_admin_client: DynamicClient) -> ClusterServiceVersion:
    """Find the CNV CSV whose deployments carry the CDI image env vars.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.

    Returns:
        ClusterServiceVersion: The CSV owning the CDI operator deployment.

    Raises:
        ValueError: If no active matching CSV is found, or if multiple are (ambiguous target).
    """
    # During an operator upgrade a replaced CSV can linger with the same deployments;
    # patching it would leave the running CDI deployment unchanged, so only the active
    # (Succeeded-phase) CSV qualifies.
    candidates = [
        candidate
        for candidate in ClusterServiceVersion.get(client=ocp_admin_client, namespace=CNV_NAMESPACE)
        if _csv_has_cdi_deployment(csv_dict=candidate.instance.to_dict())
        and candidate.instance.status.get("phase") == "Succeeded"
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ValueError(
            f"Multiple active CNV CSVs with a CDI deployment found in namespace "
            f"'{CNV_NAMESPACE}': {sorted(candidate.name for candidate in candidates)}"
        )

    raise ValueError(f"No active CNV CSV with a CDI deployment found in namespace '{CNV_NAMESPACE}'")


def _csv_has_cdi_deployment(csv_dict: dict[str, Any]) -> bool:
    """Report whether a CSV carries a deployment container with both CDI image env vars.

    Args:
        csv_dict (dict[str, Any]): Full CNV CSV resource dict.

    Returns:
        bool: True when a deployment container has CONTROLLER_IMAGE and IMPORTER_IMAGE env vars.
    """
    for deployment in csv_dict["spec"]["install"]["spec"]["deployments"]:
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            env_names = {env_var["name"] for env_var in container.get("env", [])}
            if {"CONTROLLER_IMAGE", "IMPORTER_IMAGE"}.issubset(env_names):
                return True

    return False


def _patched_cdi_csv_deployments(
    csv_dict: dict[str, Any],
    controller_image: str,
    importer_image: str,
) -> list[dict[str, Any]]:
    """Return a copy of the CSV deployments with the CDI operator images overridden.

    Args:
        csv_dict (dict[str, Any]): Full CNV CSV resource dict.
        controller_image (str): CDI controller image override.
        importer_image (str): CDI importer image override.

    Returns:
        list[dict[str, Any]]: The patched deployments list (the caller's dict is not mutated).

    Raises:
        ValueError: If no deployment container carries both CDI image env vars.
    """
    deployments = copy.deepcopy(csv_dict["spec"]["install"]["spec"]["deployments"])
    for deployment in deployments:
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            env = {env_var["name"]: env_var for env_var in container.get("env", [])}
            if "CONTROLLER_IMAGE" in env and "IMPORTER_IMAGE" in env:
                env["CONTROLLER_IMAGE"]["value"] = controller_image
                env["IMPORTER_IMAGE"]["value"] = importer_image
                return deployments

    raise ValueError("No CDI deployment container with CONTROLLER_IMAGE/IMPORTER_IMAGE found in the CNV CSV")


def _wait_for_deployment_image(deployment: Deployment, expected_image: str, timeout: int) -> None:
    """Wait until a deployment's first container runs the expected image.

    Args:
        deployment (Deployment): The deployment to poll.
        expected_image (str): The image the first container must run.
        timeout (int): Poll timeout in seconds.

    Raises:
        TimeoutExpiredError: If the deployment does not run the expected image in time.
    """
    for image in TimeoutSampler(
        wait_timeout=timeout,
        sleep=10,
        func=lambda: str(deployment.instance.spec.template.spec.containers[0].image),
    ):
        if image == expected_image:
            break


def _wait_for_cdi_rollout(ocp_admin_client: DynamicClient, controller_image: str) -> None:
    """Wait until the CDI controller deployment runs the overridden image and is rolled out.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        controller_image (str): The expected CDI controller image.

    Raises:
        TimeoutExpiredError: If the CDI deployment does not roll out or become available in time.
    """
    cdi_deployment = _get_cdi_controller_deployment(ocp_admin_client=ocp_admin_client)
    _wait_for_deployment_image(
        deployment=cdi_deployment,
        expected_image=controller_image,
        timeout=CDI_ROLLOUT_TIMEOUT,
    )

    # The image check above reads the desired pod template; immediately after the CSV patch the
    # Available condition and the running pods can still reflect the previous replicaset. Wait
    # until the deployment status observes the current generation with all replicas updated —
    # only then is the overridden CDI controller actually serving.
    for observed_generation, updated_replicas, replicas, generation in TimeoutSampler(
        wait_timeout=CDI_ROLLOUT_TIMEOUT,
        sleep=10,
        func=lambda: (
            cdi_deployment.instance.status.get("observedGeneration", 0),
            cdi_deployment.instance.status.get("updatedReplicas", 0),
            cdi_deployment.instance.spec.get("replicas", 1),
            cdi_deployment.instance.metadata.get("generation", 0),
        ),
    ):
        if observed_generation >= generation and updated_replicas == replicas:
            break

    cdi_deployment.wait_for_condition(condition="Available", status="True", timeout=CDI_ROLLOUT_TIMEOUT)


def _wait_for_provider_copy_appliance_check(provider: Provider) -> None:
    """Wait until the provider's copy appliance check passes.

    The check clones the template, boots an appliance, and sets it up. The verdict is recorded
    on the durable CopyApplianceChecked condition (True even when the check FAILED), while the
    blocking CopyApplianceCheckNotReady condition is set both while the check is pending
    (reason CopyApplianceCheckPending, e.g. while the template is still building) and when it
    failed (reason CopyApplianceCheckFailed). The wait therefore fails fast only on the failed
    reason and otherwise samples until the checked condition arrives without the blocker.

    Args:
        provider (Provider): The source Provider OCP resource.

    Raises:
        TimeoutExpiredError: If the check does not complete in time.
        ValueError: If the check completed with a failure.
    """
    for conditions in TimeoutSampler(
        wait_timeout=COPY_APPLIANCE_CHECK_TIMEOUT,
        sleep=10,
        func=lambda: {condition["type"]: condition for condition in provider.instance.status.get("conditions", [])},
    ):
        checked = conditions.get("CopyApplianceChecked")
        blocked = conditions.get("CopyApplianceCheckNotReady")
        if blocked and blocked.get("status") == "True" and "Failed" in str(blocked.get("reason", "")):
            raise ValueError(f"Copy appliance check failed: {blocked.get('message')}")
        if checked and checked.get("status") == "True" and not blocked:
            return


def _wait_for_provider_copy_appliance_secrets(provider: Provider) -> None:
    """Wait until the provider status names its copy appliance SSH secrets.

    The provider controller generates the copy appliance SSH keypair secrets asynchronously
    once the copyAppliance settings are set. The CAT controller fails terminally when it
    reconciles before those secrets exist, so the wait must precede the CAT creation.

    Args:
        provider (Provider): The source Provider OCP resource.

    Raises:
        TimeoutExpiredError: If the provider does not name its copy appliance SSH secrets in
            time.
    """
    for public_secret in TimeoutSampler(
        wait_timeout=PROVIDER_COPY_APPLIANCE_SECRETS_TIMEOUT,
        sleep=5,
        func=lambda: provider.instance.status.get("copyApplianceSSHPublicSecret"),
    ):
        if public_secret:
            break


@dataclass
class ToeholdClusterState:
    """Cluster-wide changes applied by the toehold setup, in restore order.

    Each field is filled in as the matching change is applied, so a setup that fails part-way
    still restores everything that was already applied instead of leaving the cluster on the
    dev CDI images or the appliance template config.

    Attributes:
        settings_editor (ResourceEditor | None): Provider settings editor, once applied.
        cdi_override (CdiImageOverride | None): CDI image override, once applied.
        controller_editor (ResourceEditor | None): Controller template-config editor, once applied.
    """

    settings_editor: ResourceEditor | None = field(default=None)
    cdi_override: CdiImageOverride | None = field(default=None)
    controller_editor: ResourceEditor | None = field(default=None)


def apply_toehold_setup(
    ocp_admin_client: DynamicClient,
    ocp_provider: Provider,
    toehold_config: dict[str, Any],
    fixture_store: dict[str, Any],
    target_namespace: str,
    mtv_namespace: str,
    state: ToeholdClusterState,
) -> None:
    """Apply every cluster-wide toehold change, cheapest fail-fast checks first.

    The CDI image override rolls the cluster-wide CDI controller and waits up to three
    rollouts, so it runs only after the provider/controller VDDK checks and the leftover
    template check have passed: a misconfiguration fails in seconds instead of after a CDI
    rollout plus its rollback, and the cluster's CDI is not disturbed for nothing.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        ocp_provider (Provider): The session's source Provider OCP resource.
        toehold_config (dict[str, Any]): Validated toehold configuration.
        fixture_store (dict[str, Any]): Session fixture store for resource tracking.
        target_namespace (str): Namespace for the migration resources.
        mtv_namespace (str): Namespace where the ForkliftController is installed.
        state (ToeholdClusterState): Sink updated as each change is applied, so the caller can
            restore a partially applied setup.

    Raises:
        ValueError: If the appliance path is disabled, or a leftover template exists.
    """
    # Provider settings first: the assertion below reads them back from the cluster.
    state.settings_editor = patch_provider_copy_appliance_settings(
        provider=ocp_provider,
        toehold_config=toehold_config,
    )
    state.settings_editor.update(backup_resources=True)
    assert_copy_appliance_path_enabled(
        provider=ocp_provider,
        ocp_admin_client=ocp_admin_client,
        mtv_namespace=mtv_namespace,
    )
    assert_copy_appliance_template_absent(
        ocp_admin_client=ocp_admin_client,
        provider=ocp_provider,
        template_name=copy_appliance_template_name(provider=ocp_provider),
    )

    # Cluster-wide overrides: CDI images first so the appliance import path is in place before
    # the feature gate lets the controller reconcile a provider template.
    state.cdi_override = patch_cdi_images(ocp_admin_client=ocp_admin_client, toehold_config=toehold_config)
    state.controller_editor = patch_forklift_controller(
        ocp_admin_client=ocp_admin_client,
        mtv_namespace=mtv_namespace,
        toehold_config=toehold_config,
    )

    # Appliance resources: pull permission (MTV-7457 workaround), template, then wait for it.
    create_copy_appliance_nbd_puller_rolebinding(
        ocp_admin_client=ocp_admin_client,
        fixture_store=fixture_store,
        mtv_namespace=mtv_namespace,
        target_namespace=target_namespace,
    )
    wait_copy_appliance_template_ready(
        copy_appliance_template=create_provider_copy_appliance_template(
            ocp_admin_client=ocp_admin_client,
            toehold_config=toehold_config,
            fixture_store=fixture_store,
            provider=ocp_provider,
        ),
        provider=ocp_provider,
    )


def _provider_exists(ocp_admin_client: DynamicClient, provider: Provider) -> bool:
    """Return whether the provider resource still exists in the cluster.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        provider (Provider): The provider to look for.

    Returns:
        bool: True when a provider with that name is present in its namespace.
    """
    return any(
        existing.name == provider.name
        for existing in Provider.get(client=ocp_admin_client, namespace=provider.namespace)
    )


def restore_toehold_setup(
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
    state: ToeholdClusterState,
    provider: Provider | None = None,
) -> None:
    """Restore every toehold change, innermost first, without skipping later restores.

    Nested try/finally so each restore runs even when an earlier one fails: the cluster-wide CDI
    override must not survive a settings-restore failure, and the controller restore must not
    depend on the optional CDI override existing. The first raised error propagates once all
    restores have been attempted.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the ForkliftController is installed.
        state (ToeholdClusterState): Changes actually applied by the setup; unset fields are
            skipped, so a partially applied setup restores only what it changed.
        provider (Provider | None): The session provider; its settings restore is skipped when
            session cleanup already deleted it (nothing left to restore).
    """
    try:
        if state.settings_editor and (
            provider is None or _provider_exists(ocp_admin_client=ocp_admin_client, provider=provider)
        ):
            state.settings_editor.restore()
    finally:
        # Waits until the original CDI controller is serving again, so a subsequent regular
        # run cannot start while the overridden controller is still live.
        try:
            if state.cdi_override:
                state.cdi_override.restore(ocp_admin_client=ocp_admin_client)
        finally:
            if state.controller_editor:
                # Waits until the operator has applied the restored template config, so a
                # following regular run cannot start against the toehold settings.
                restore_forklift_controller(
                    editor=state.controller_editor,
                    ocp_admin_client=ocp_admin_client,
                    mtv_namespace=mtv_namespace,
                )
