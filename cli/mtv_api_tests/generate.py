"""Generate subcommand — interactive wizard for .providers.json and job.yaml."""

from __future__ import annotations

import json
import shlex
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import typer
from dotenv import load_dotenv
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from cli.mtv_api_tests.common import (
    JOB_YAML_PATH,
    _env_or_prompt,
    _write_secret_file,
    build_ocp_provider,
    build_providers_json,
    connect_ocp,
    connect_vsphere,
    console,
    discover_datastores,
    discover_esxi_hosts,
    discover_storage_classes,
    discover_vms,
    disconnect_vsphere,
    gather_vendor_fields,
    generate_job_yaml,
    get_ocp_credentials,
    get_providers_json_path,
    get_ocp_version,
    get_storage_credentials,
    get_vsphere_credentials,
    get_vsphere_version,
    mask_passwords,
    prompt_test_filter,
    select_category,
    select_from_list,
    select_vendor,
    validate_mtv_installed,
    write_providers_json,
)


@contextmanager
def _vsphere_session(host: str, user: str, password: str, ssl_config: dict[str, Any] | None) -> Generator:
    """Context manager for vSphere connection with automatic cleanup.

    Args:
        host: vCenter hostname.
        user: vSphere username.
        password: vSphere password.
        ssl_config: SSL configuration dict with 'verify_ssl' and optional 'ca_bundle'.
    """
    si = connect_vsphere(host, user, password, ssl_config=ssl_config)
    try:
        yield si
    finally:
        disconnect_vsphere(si)


# ---------------------------------------------------------------------------
# Generate sub-flows
# ---------------------------------------------------------------------------


def _gather_vsphere_resources(
    vsphere_host: str, vsphere_user: str, vsphere_pass: str, ssl_config: dict[str, str] | None = None
) -> dict[str, Any]:
    """Connect to vSphere, discover resources, and return user selections.

    Args:
        vsphere_host: vCenter hostname.
        vsphere_user: vSphere username.
        vsphere_pass: vSphere password.
        ssl_config: SSL configuration dict with 'verify_ssl' and optional 'ca_bundle'.

    Returns:
        Dict with keys: version, primary_ds, secondary_ds, selected_vm, selected_host.

    Raises:
        typer.Exit: If connection fails.
    """
    try:
        with _vsphere_session(vsphere_host, vsphere_user, vsphere_pass, ssl_config=ssl_config) as si:
            content = si.RetrieveContent()
            version = get_vsphere_version(content)
            console.print(f"  [dim]vSphere version:[/dim] {version}")

            # Datastores
            console.print("\n[bold]Datastore Selection[/bold]")
            datastores = discover_datastores(content)
            ds_columns = ["name", "id", "type", "capacity_gb", "free_gb"]
            primary_ds = select_from_list(datastores, ds_columns, "Available Datastores", "Select PRIMARY datastore")

            secondary_ds: dict[str, Any] | None = None
            if Confirm.ask("  Add a secondary datastore for multi-datastore tests?", default=False):
                remaining = [d for d in datastores if d["id"] != primary_ds["id"]]
                if remaining:
                    secondary_ds = select_from_list(
                        remaining, ds_columns, "Available Datastores (excluding primary)", "Select SECONDARY datastore"
                    )
                else:
                    console.print("  [yellow]No other datastores available[/yellow]")

            # VMs
            console.print("\n[bold]Test VM Selection[/bold]")
            vms = discover_vms(content)
            selected_vm = select_from_list(
                vms, ["name", "power_state", "guest_os"], "Available VMs", "Select default test VM"
            )

            # ESXi hosts
            console.print("\n[bold]ESXi Host Selection[/bold]")
            esxi_hosts = discover_esxi_hosts(content)
            selected_host = select_from_list(
                esxi_hosts, ["name", "connection_state", "power_state"], "Available ESXi Hosts", "Select ESXi host"
            )
    except ConnectionError as exc:
        console.print(f"\n[red]Error: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    return {
        "version": version,
        "primary_ds": primary_ds,
        "secondary_ds": secondary_ds,
        "selected_vm": selected_vm,
        "selected_host": selected_host,
    }


def _gather_copyoffload_config(
    vsphere_resources: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    """Gather all copy-offload configuration interactively.

    Args:
        vsphere_resources: Dict from _gather_vsphere_resources().

    Returns:
        Tuple of (copyoffload_config, guest_linux_user, guest_linux_password).
    """
    selected_host = vsphere_resources["selected_host"]

    # Clone method
    console.print("\n[bold]Clone Method[/bold]")
    clone_method = Prompt.ask("  Clone method", choices=["ssh", "vib"], default="ssh")
    esxi_config: dict[str, str] = {"esxi_clone_method": clone_method, "esxi_host": selected_host["name"]}
    if clone_method == "ssh":
        esxi_config["esxi_user"] = _env_or_prompt("COPYOFFLOAD_ESXI_USER", "ESXi SSH username")
        esxi_config["esxi_password"] = _env_or_prompt("COPYOFFLOAD_ESXI_PASSWORD", "ESXi SSH password", password=True)

    # Storage vendor and fields
    vendor = select_vendor()
    vendor_fields = gather_vendor_fields(vendor)

    # Storage credentials
    storage_host, storage_user, storage_pass = get_storage_credentials()

    # Guest VM credentials
    console.print("\n[bold]Guest VM Credentials[/bold]")
    guest_linux_user = Prompt.ask("  Linux VM username", default="root")
    guest_linux_pass = Prompt.ask("  Linux VM password", password=True)

    # Optional RDM
    rdm_lun_uuid: str | None = None
    if Confirm.ask("\n  Configure RDM LUN UUID? (for RDM disk tests)", default=False):
        rdm_lun_uuid = Prompt.ask("  RDM LUN UUID (e.g. naa.xxx)")

    # Optional non-XCOPY datastore
    non_xcopy_ds_id: str | None = None
    if Confirm.ask("\n  Configure non-XCOPY datastore? (for fallback/comparison tests)", default=False):
        non_xcopy_ds_id = Prompt.ask("  Non-XCOPY datastore ID")

    # Build config
    config: dict[str, Any] = {
        "storage_vendor_product": vendor,
        "datastore_id": vsphere_resources["primary_ds"]["id"],
        "default_vm_name": vsphere_resources["selected_vm"]["name"],
        "storage_hostname": storage_host,
        "storage_username": storage_user,
        "storage_password": storage_pass,
        **esxi_config,
        **vendor_fields,
    }
    if vsphere_resources["secondary_ds"]:
        config["secondary_datastore_id"] = vsphere_resources["secondary_ds"]["id"]
    if rdm_lun_uuid:
        config["rdm_lun_uuid"] = rdm_lun_uuid
    if non_xcopy_ds_id:
        config["non_xcopy_datastore_id"] = non_xcopy_ds_id

    return config, guest_linux_user, guest_linux_pass


def _gather_ocp_storage_class() -> tuple[dict[str, str], str, str]:
    """Gather OCP credentials, discover storage classes and cluster version.

    Connects to the cluster, validates MTV, discovers storage classes,
    and retrieves the OCP version.

    Returns:
        Tuple of (ocp_credentials, selected_storage_class_name, ocp_version).

    Raises:
        typer.Exit: If connection fails.
    """
    ocp_creds = get_ocp_credentials()

    try:
        ocp_client = connect_ocp(ocp_creds)
    except ConnectionError as exc:
        console.print(f"\n[red]Error: {exc}[/red]")
        console.print("[yellow]Cannot continue without OCP connection.[/yellow]")
        raise typer.Exit(code=1) from exc

    # Cluster version
    ocp_version = get_ocp_version(ocp_client)
    console.print(f"  [dim]OCP version:[/dim] {ocp_version}")

    # Validate MTV
    if validate_mtv_installed(ocp_client):
        console.print("  [green]MTV (ForkliftController) is installed[/green]")
    else:
        console.print("  [yellow]Warning: MTV (ForkliftController) not found in openshift-mtv namespace[/yellow]")

    # Storage classes
    console.print("\n[bold]Storage Class Selection[/bold]")
    scs = discover_storage_classes(ocp_client)
    if scs:
        selected_sc = select_from_list(
            scs,
            ["name", "provisioner", "is_default"],
            "Available Storage Classes",
            "Select block storage class",
        )
        storage_class = selected_sc["name"]
    else:
        storage_class = Prompt.ask("  No storage classes found. Enter storage class name manually")

    # Gather cluster credentials for the Job (username/password required for OAuth bearer token)
    server = ocp_client.configuration.host
    if "kubeconfig" in ocp_creds:
        console.print("\n[bold]Cluster Credentials for Job[/bold]")
        console.print("  [dim]The Job needs username/password for bearer token auth.[/dim]")
        host = Prompt.ask("  Cluster API URL", default=server)
        username = Prompt.ask("  Cluster username", default="kubeadmin")
        password = Prompt.ask("  Cluster password", password=True)
        job_creds: dict[str, str] = {"host": host, "username": username, "password": password}
        if "verify_ssl" in ocp_creds:
            job_creds["verify_ssl"] = ocp_creds["verify_ssl"]
        if "ca_bundle" in ocp_creds:
            job_creds["ca_bundle"] = ocp_creds["ca_bundle"]
    else:
        job_creds = ocp_creds

    return job_creds, storage_class, ocp_version


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def generate_command(image: str, category: str) -> None:
    """Interactive wizard to generate .providers.json and mtv-api-tests-manifests.yaml for MTV tests.

    Args:
        image: Container image for OCP Job.
        category: Pre-selected test category (empty for interactive).

    Raises:
        typer.Exit: On user abort, invalid input, or connection failure.
    """
    load_dotenv()

    console.print(
        Panel(
            "[bold]MTV Test Configuration Wizard[/bold]\n\n"
            "This tool will guide you through configuring tests by discovering\n"
            "resources from your vSphere and OpenShift environments.",
            title="MTV API Tests",
            border_style="blue",
        )
    )

    resolved_category = select_category(category)

    test_filter = prompt_test_filter("")

    # OCP discovery (first — validates cluster access)
    ocp_creds, storage_class, ocp_version = _gather_ocp_storage_class()

    # vSphere discovery
    vsphere_host, vsphere_user, vsphere_pass, vsphere_ssl = get_vsphere_credentials()
    vsphere_resources = _gather_vsphere_resources(vsphere_host, vsphere_user, vsphere_pass, ssl_config=vsphere_ssl)

    # Copy-offload config
    copyoffload_config, guest_linux_user, guest_linux_pass = _gather_copyoffload_config(vsphere_resources)

    # Build provider configs
    vsphere_key, vsphere_config = build_providers_json(
        vsphere_host=vsphere_host,
        vsphere_user=vsphere_user,
        vsphere_pass=vsphere_pass,
        version=vsphere_resources["version"],
        guest_linux_user=guest_linux_user,
        guest_linux_pass=guest_linux_pass,
        copyoffload_config=copyoffload_config,
    )
    ocp_key, ocp_config = build_ocp_provider(
        ocp_creds=ocp_creds,
        ocp_version=ocp_version,
        storage_class=storage_class,
    )

    # Show summary
    all_providers = {vsphere_key: mask_passwords(vsphere_config), ocp_key: mask_passwords(ocp_config)}
    console.print("\n")
    console.print(
        Panel(
            json.dumps(all_providers, indent=2),
            title="Configuration Summary",
            border_style="green",
        )
    )
    console.print(f"\n  Source provider: [bold]{vsphere_key}[/bold]")
    console.print(f"  OCP provider: [bold]{ocp_key}[/bold]")
    console.print(f"  Storage class: [bold]{storage_class}[/bold]")

    if not Confirm.ask("\n  Write configuration files?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit()

    # Write provider entries (both vSphere and OCP)
    if not write_providers_json(vsphere_key, vsphere_config):
        console.print("[dim]vSphere provider unchanged.[/dim]")
    if not write_providers_json(ocp_key, ocp_config):
        console.print("[dim]OCP provider unchanged.[/dim]")

    # Read back the full providers.json for the Job manifest
    providers_json_content = get_providers_json_path().read_text()
    job_yaml, namespace, job_name = generate_job_yaml(
        provider_key=vsphere_key,
        storage_class=storage_class,
        category=resolved_category,
        image=image,
        ocp_creds=ocp_creds,
        providers_json_content=providers_json_content,
        test_filter=test_filter,
    )
    _write_secret_file(JOB_YAML_PATH, job_yaml)
    console.print(f"  [green]Wrote {JOB_YAML_PATH}[/green]")
    console.print(f"  [bold]Namespace:[/bold] {namespace}")

    local_cmd = f"mtv-api-tests run --mode local --source-provider {vsphere_key} --destination-provider {ocp_key} --storage-class {storage_class}"
    if resolved_category != "all":
        local_cmd += f" --category {resolved_category}"
    if test_filter:
        local_cmd += f" --test-filter {shlex.quote(test_filter)}"

    console.print(
        Panel(
            f"[bold]Run tests locally:[/bold]\n"
            f"  {local_cmd}\n\n"
            f"[bold]Run tests as OCP Job:[/bold]\n"
            f"  mtv-api-tests run --mode job\n\n"
            f"[bold]Follow Job logs:[/bold]\n"
            f"  oc logs -f -n {namespace} job/{job_name}",
            title="Next Steps",
            border_style="blue",
        )
    )
