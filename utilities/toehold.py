# Toehold (Copy Appliance) disk transfer: MTV deploys a copy appliance VM inside vSphere and
# exposes the disks through it over NBD instead of using VDDK (VIRTSTRAT-757, MTV 5.0).

from __future__ import annotations

import copy
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
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

CNV_NAMESPACE = "openshift-cnv"
CDI_CONTROLLER_DEPLOYMENT = "cdi-deployment"

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
    "datastore": "datastore1",
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
    },
    "vsphere-8.0.1": {
        **_TOEHOLD_SHARED_CONFIG,
        "resource_pool": "/Datacenter/host/mtv-cluster/Resources",
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
    if builder_image := toehold_config.get("builder_image"):
        patches["copy_appliance_template_builder_image_fqin"] = builder_image
    if cpu := toehold_config.get("cpu"):
        patches["copy_appliance_template_cpu"] = str(cpu)
    if memory_mib := toehold_config.get("memory_mib"):
        patches["copy_appliance_template_memory_mib"] = str(memory_mib)

    forklift_controller = ForkliftController(
        client=ocp_admin_client,
        name=FORKLIFT_CONTROLLER_NAME,
        namespace=mtv_namespace,
        ensure_exists=True,
    )
    LOGGER.info(f"Patching the ForkliftController copy appliance config: {sorted(patches)}")
    editor = ResourceEditor(patches={forklift_controller: {"spec": patches}})
    editor.update(backup_resources=True)

    try:
        forklift_controller.wait_for_condition(
            status=forklift_controller.Condition.Status.TRUE,
            condition=forklift_controller.Condition.Type.RUNNING,
            timeout=FORKLIFT_CONTROLLER_ROLLOUT_TIMEOUT,
        )
    except TimeoutExpiredError:
        # A cluster-wide controller override must not survive a failed rollout.
        editor.restore()
        raise
    return editor


def patch_provider_copy_appliance_settings(
    provider: Provider,
    toehold_config: dict[str, Any],
) -> ResourceEditor:
    """Patch the provider's copy appliance placement settings, with backup for restore.

    The provider controller's template sync fills the CopyApplianceTemplate spec from the
    provider's copyAppliance settings and the controller's template config, so the settings
    must be in place before the feature is enabled on the controller.

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
                        "copyApplianceDatastore": toehold_config["datastore"],
                        "copyApplianceFolder": toehold_config["folder"],
                        "copyApplianceNetwork": toehold_config["network"],
                        "copyApplianceResourcePool": toehold_config["resource_pool"],
                    }
                }
            }
        }
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
    """
    _wait_for_provider_copy_appliance_secrets(provider=provider)

    template_name = f"{provider.name}-copy-appliance-template"
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
) -> CdiImageOverride | None:
    """Override the CDI controller and importer images on the CNV CSV when the config provides them.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        toehold_config (dict[str, Any]): Validated toehold configuration.

    Returns:
        CdiImageOverride | None: The override holding the backup and rollback wait, or None
            when the config carries no CDI image keys (the cluster is expected to be prepared
            already).

    Raises:
        ValueError: If no CNV CSV with a CDI deployment is found.
    """
    controller_image = toehold_config.get("cdi_controller_image")
    importer_image = toehold_config.get("cdi_importer_image")
    if not controller_image or not importer_image:
        return None

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
    try:
        _wait_for_cdi_rollout(
            ocp_admin_client=ocp_admin_client,
            controller_image=controller_image,
        )
    except TimeoutExpiredError:
        # A cluster-wide CDI override must not survive a failed rollout.
        cdi_override.restore(ocp_admin_client=ocp_admin_client)
        raise
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
