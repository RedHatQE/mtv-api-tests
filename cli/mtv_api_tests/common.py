"""Shared constants, helpers, and discovery functions for the MTV CLI tool."""

from __future__ import annotations

import base64
import json
import os
import shlex
import ssl
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
import urllib3
from ocp_resources.forklift_controller import ForkliftController
from ocp_utilities.infra import get_client
from ocp_resources.storage_class import StorageClass
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from utilities.utils import DEFAULT_PROVIDERS_JSON_PATH

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

console = Console()
JOB_YAML_PATH = Path("mtv-api-tests-manifests.yaml")
DEFAULT_TEST_IMAGE = "ghcr.io/redhatqe/mtv-api-tests:latest"


def _generate_namespace_name() -> str:
    """Generate a unique namespace name with a short UUID suffix.

    Returns:
        Namespace name like 'mtv-tests-a1b2c3'.
    """
    short_id = uuid.uuid4().hex[:6]
    return f"mtv-tests-{short_id}"


SUPPORTED_VENDORS: dict[str, dict[str, Any]] = {
    "ontap": {"label": "NetApp ONTAP", "fields": [("ontap_svm", "SVM/vServer name")]},
    "vantara": {
        "label": "Hitachi Vantara",
        "fields": [
            ("vantara_storage_id", "Storage array serial number"),
            ("vantara_storage_port", "Storage API port"),
            ("vantara_hostgroup_id_list", "Host group IDs (e.g. CL1-A,1:CL2-B,2)"),
        ],
    },
    "pureFlashArray": {
        "label": "Pure Storage FlashArray",
        "fields": [("pure_cluster_prefix", "Cluster prefix (px_...)")],
    },
    "powerflex": {"label": "Dell PowerFlex", "fields": [("powerflex_system_id", "PowerFlex system ID")]},
    "powermax": {"label": "Dell PowerMax", "fields": [("powermax_symmetrix_id", "Symmetrix ID")]},
    "powerstore": {"label": "Dell PowerStore", "fields": []},
    "primera3par": {"label": "HPE Primera/3PAR", "fields": []},
    "infinibox": {"label": "Infinidat InfiniBox", "fields": []},
    "flashsystem": {"label": "IBM FlashSystem", "fields": []},
}

TEST_CATEGORIES: dict[str, str] = {
    "all": "All tests (no marker filter)",
    "copyoffload": "Copy-offload (XCOPY) tests",
    "tier0": "Core functionality tests (smoke tests)",
    "warm": "Warm migration tests",
    "remote": "Remote cluster migration tests",
}


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


def select_category(category: str) -> str:
    """Resolve test category from user input or interactive selection.

    Args:
        category: User-provided category (may be empty).

    Returns:
        Resolved category key (pytest marker name).

    Raises:
        typer.Exit: If category is invalid.
    """
    if category and category not in TEST_CATEGORIES:
        console.print(f"[red]Error: Unknown category '{category}'. Available: {', '.join(TEST_CATEGORIES)}[/red]")
        raise typer.Exit(code=1)
    if category:
        return category

    console.print("\n[bold]Test Category (pytest marker)[/bold]")
    categories = list(TEST_CATEGORIES.items())
    for idx, (key, description) in enumerate(categories, 1):
        console.print(f"  {idx}. [bold]{key}[/bold] - {description}")
    console.print("  [dim]Tip: use -k filter in 'run' to narrow to specific tests (e.g. MTV-559, thin)[/dim]")
    while True:
        choice = IntPrompt.ask("  Select category", default=1)
        if 1 <= choice <= len(categories):
            selected = categories[choice - 1][0]
            console.print(f"  [green]Selected:[/green] {selected}")
            return selected
        console.print(f"  [red]Invalid choice. Enter 1-{len(categories)}[/red]")


def prompt_test_filter(test_filter: str) -> str:
    """Prompt for pytest -k filter if not already provided.

    Args:
        test_filter: Pre-provided filter (empty for interactive).

    Returns:
        The filter string (may be empty if user skips).
    """
    if test_filter:
        return test_filter
    console.print("\n  [dim]Filter examples: 'MTV-559', 'thin', 'thin or thick', 'copyoffload and not rdm'[/dim]")
    return Prompt.ask("  Run specific tests? (-k filter, Enter to skip)", default="")


def _display_table(items: list[dict[str, Any]], columns: list[str], title: str) -> None:
    """Display items in a Rich table with numbered rows.

    Args:
        items: List of dicts to display.
        columns: Keys to show as columns.
        title: Table title.
    """
    table = Table(title=title, show_lines=True)
    table.add_column("#", style="bold cyan", width=4)
    for col in columns:
        table.add_column(col.replace("_", " ").title())

    for idx, item in enumerate(items, 1):
        row = [str(idx)]
        for col in columns:
            row.append(str(item.get(col, "")))
        table.add_row(*row)

    console.print(table)


def select_from_list(items: list[dict[str, Any]], columns: list[str], title: str, prompt_text: str) -> dict[str, Any]:
    """Display a table and let the user select one item by number.

    Supports filtering: if the list has more than 20 items, prompts for a filter
    string first. The user can type a substring to narrow down the list, or press
    Enter to see all items.

    Args:
        items: List of dicts to choose from.
        columns: Keys to display as columns.
        title: Table title.
        prompt_text: Prompt text for selection.

    Returns:
        The selected item dict.

    Raises:
        ValueError: If no items available.
    """
    if not items:
        raise ValueError(f"No items found for '{title}'")

    filtered = items
    if len(items) > 20:
        console.print(f"  [dim]{len(items)} items found. Type to filter or press Enter to show all.[/dim]")
        filter_text = Prompt.ask("  Filter", default="")
        if filter_text:
            filter_lower = filter_text.lower()
            filtered = [
                item for item in items if any(filter_lower in str(item.get(col, "")).lower() for col in columns)
            ]
            if not filtered:
                console.print(f"  [yellow]No matches for '{filter_text}'. Showing all items.[/yellow]")
                filtered = items

    _display_table(filtered, columns, title)
    while True:
        choice = IntPrompt.ask(f"  {prompt_text}", default=1)
        if 1 <= choice <= len(filtered):
            selected = filtered[choice - 1]
            console.print(f"  [green]Selected:[/green] {selected[columns[0]]}")
            return selected
        console.print(f"  [red]Invalid choice. Enter 1-{len(filtered)}[/red]")


def select_vendor() -> str:
    """Show supported storage vendors and let user select one.

    Returns:
        Vendor key string (e.g. 'ontap').
    """
    console.print("\n[bold]Storage Vendor Selection[/bold]")
    vendors = list(SUPPORTED_VENDORS.items())
    table = Table(title="Supported Storage Vendors", show_lines=True)
    table.add_column("#", style="bold cyan", width=4)
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Extra Fields")

    for idx, (key, info) in enumerate(vendors, 1):
        fields = ", ".join(f[0] for f in info["fields"]) if info["fields"] else "none"
        table.add_row(str(idx), key, info["label"], fields)

    console.print(table)
    while True:
        choice = IntPrompt.ask("  Select vendor", default=1)
        if 1 <= choice <= len(vendors):
            vendor_key = vendors[choice - 1][0]
            console.print(f"  [green]Selected:[/green] {SUPPORTED_VENDORS[vendor_key]['label']}")
            return vendor_key
        console.print(f"  [red]Invalid choice. Enter 1-{len(vendors)}[/red]")


def gather_vendor_fields(vendor: str) -> dict[str, str]:
    """Prompt for vendor-specific configuration fields.

    Args:
        vendor: Vendor key from SUPPORTED_VENDORS.

    Returns:
        Dict of field_name -> value.
    """
    fields = SUPPORTED_VENDORS[vendor]["fields"]
    if not fields:
        console.print(f"  [dim]No extra fields needed for {SUPPORTED_VENDORS[vendor]['label']}[/dim]")
        return {}

    console.print(f"\n[bold]{SUPPORTED_VENDORS[vendor]['label']} Configuration[/bold]")
    result: dict[str, str] = {}
    for field_name, description in fields:
        result[field_name] = Prompt.ask(f"  {description} ({field_name})")
    return result


# ---------------------------------------------------------------------------
# Credential gathering
# ---------------------------------------------------------------------------


def _env_or_prompt(env_var: str, prompt_text: str, *, password: bool = False) -> str:
    """Return value from env var or prompt the user interactively.

    Args:
        env_var: Environment variable name to check first.
        prompt_text: Text to show when prompting.
        password: Whether to hide input.

    Returns:
        The credential value.
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        display = "***" if password else value
        console.print(f"  [dim]{prompt_text}:[/dim] {display} [dim](from {env_var})[/dim]")
        return value
    return Prompt.ask(f"  {prompt_text}", password=password)


def _get_ssl_config(env_prefix: str) -> dict[str, str]:
    """Prompt for SSL verification and optional CA bundle.

    Checks ``{env_prefix}_VERIFY_SSL`` and ``{env_prefix}_CA_BUNDLE`` env vars
    first, then prompts interactively.

    Args:
        env_prefix: Environment variable prefix (e.g. 'VSPHERE', 'CLUSTER').

    Returns:
        Dict with 'verify_ssl' ('true'/'false') and optional 'ca_bundle' path.

    Raises:
        typer.Exit: If verify_ssl is enabled and the specified CA bundle path does not exist.
    """
    verify_env = os.environ.get(f"{env_prefix}_VERIFY_SSL", "").strip().lower()
    if verify_env:
        verify_ssl = verify_env in ("true", "1", "yes")
        console.print(f"  [dim]SSL verification:[/dim] {verify_ssl} [dim](from {env_prefix}_VERIFY_SSL)[/dim]")
    else:
        verify_ssl = Confirm.ask("  Verify SSL certificates?", default=True)

    if not verify_ssl:
        console.print(
            "  [yellow]Warning: SSL verification disabled — connections will not validate certificates[/yellow]"
        )

    result: dict[str, str] = {"verify_ssl": str(verify_ssl).lower()}

    if verify_ssl:
        ca_env = os.environ.get(f"{env_prefix}_CA_BUNDLE", "").strip()
        if ca_env:
            console.print(f"  [dim]CA bundle:[/dim] {ca_env} [dim](from {env_prefix}_CA_BUNDLE)[/dim]")
            ca_bundle = ca_env
        else:
            ca_bundle = Prompt.ask("  CA bundle path (Enter to skip for system CA)", default="")
        if ca_bundle:
            if not Path(ca_bundle).exists():
                console.print(f"[red]Error: CA bundle not found at {ca_bundle}[/red]")
                raise typer.Exit(code=1)
            result["ca_bundle"] = ca_bundle

    return result


def get_vsphere_credentials() -> tuple[str, str, str, dict[str, str]]:
    """Gather vSphere credentials and SSL config from env vars or interactive prompts.

    Returns:
        Tuple of (host, username, password, ssl_config).
    """
    console.print("\n[bold]vSphere Credentials[/bold]")
    host = _env_or_prompt("VSPHERE_HOST", "vCenter hostname/IP")
    username = _env_or_prompt("VSPHERE_USERNAME", "Username")
    password = _env_or_prompt("VSPHERE_PASSWORD", "Password", password=True)
    ssl_config = _get_ssl_config("VSPHERE")
    return host, username, password, ssl_config


def get_storage_credentials() -> tuple[str, str, str]:
    """Gather storage array credentials from env vars or interactive prompts.

    Returns:
        Tuple of (hostname, username, password).
    """
    console.print("\n[bold]Storage Array Credentials[/bold]")
    console.print("  [dim]Tip: set STORAGE_HOSTNAME, STORAGE_USERNAME, STORAGE_PASSWORD env vars to skip prompts[/dim]")
    hostname = _env_or_prompt("STORAGE_HOSTNAME", "Storage hostname/IP")
    username = _env_or_prompt("STORAGE_USERNAME", "Storage username")
    password = _env_or_prompt("STORAGE_PASSWORD", "Storage password", password=True)
    return hostname, username, password


def get_ocp_credentials() -> dict[str, str]:
    """Gather OpenShift credentials from KUBECONFIG, env vars, or prompts.

    Note:
        May return ``{"kubeconfig": ...}`` without host/username/password.
        Callers that need explicit credentials (e.g. ``generate_job_yaml``)
        must handle this case by prompting for the missing fields.
        See ``generate.py:_gather_ocp_storage_class`` for an example.

    Returns:
        Dict with keys: kubeconfig (optional), host, username, password.
    """
    console.print("\n[bold]OpenShift Credentials[/bold]")
    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    if kubeconfig:
        console.print(f"  [dim]Using KUBECONFIG:[/dim] {kubeconfig}")
        return {"kubeconfig": kubeconfig}

    # Check for default kubeconfig (e.g. after 'oc login')
    default_kubeconfig = Path.home() / ".kube" / "config"
    if default_kubeconfig.exists():
        if Confirm.ask(f"  Found kubeconfig at {default_kubeconfig}. Use it?", default=True):
            os.environ["KUBECONFIG"] = str(default_kubeconfig)
            console.print(f"  [dim]Using KUBECONFIG:[/dim] {default_kubeconfig}")
            return {"kubeconfig": str(default_kubeconfig)}

    use_kubeconfig = Confirm.ask("  Do you want to provide a kubeconfig path?", default=False)
    if use_kubeconfig:
        kubeconfig = Prompt.ask("  Path to kubeconfig file")
        os.environ["KUBECONFIG"] = kubeconfig
        console.print(f"  [dim]Using KUBECONFIG:[/dim] {kubeconfig}")
        return {"kubeconfig": kubeconfig}

    host = _env_or_prompt("CLUSTER_HOST", "Cluster API URL (e.g. https://api.cluster.com:6443)")
    username = _env_or_prompt("CLUSTER_USERNAME", "Username")
    password = _env_or_prompt("CLUSTER_PASSWORD", "Password", password=True)
    ssl_config = _get_ssl_config("CLUSTER")
    return {"host": host, "username": username, "password": password, **ssl_config}


# ---------------------------------------------------------------------------
# vSphere discovery
# ---------------------------------------------------------------------------


def connect_vsphere(
    host: str, username: str, password: str, ssl_config: dict[str, str] | None = None
) -> vim.ServiceInstance:
    """Connect to vSphere and return the ServiceInstance.

    Args:
        host: vCenter hostname.
        username: vSphere username.
        password: vSphere password.
        ssl_config: SSL configuration dict with 'verify_ssl' and optional 'ca_bundle'.

    Returns:
        vSphere ServiceInstance.

    Raises:
        ConnectionError: If connection fails.
    """
    ssl_config = ssl_config or {}
    verify_ssl = ssl_config.get("verify_ssl", "true") == "true"
    ca_bundle = ssl_config.get("ca_bundle", "")

    connect_kwargs: dict[str, Any] = {"host": host, "user": username, "pwd": password, "port": 443}
    if verify_ssl and ca_bundle:
        ssl_context = ssl.create_default_context(cafile=ca_bundle)
        connect_kwargs["sslContext"] = ssl_context
    elif not verify_ssl:
        console.print("  [dim]SSL certificate validation disabled for vSphere connection[/dim]")
        connect_kwargs["disableSslCertValidation"] = True

    with console.status("[bold]Connecting to vSphere..."):
        try:
            si = SmartConnect(**connect_kwargs)
        except vim.fault.InvalidLogin as exc:
            raise ConnectionError(f"Invalid vSphere credentials for {host}") from exc
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to vSphere at {host}: {exc}") from exc
    console.print("  [green]Connected to vSphere[/green]")
    return si


def disconnect_vsphere(si: vim.ServiceInstance) -> None:
    """Disconnect from vSphere.

    Args:
        si: vSphere ServiceInstance.
    """
    Disconnect(si)


def get_vsphere_version(content: vim.ServiceInstanceContent) -> str:
    """Get vSphere version string from content.

    Args:
        content: vSphere ServiceInstanceContent.

    Returns:
        Version string (e.g. '8.0.3.00400').
    """
    return f"{content.about.version}.{content.about.build}"


def discover_datastores(content: vim.ServiceInstanceContent) -> list[dict[str, Any]]:
    """Discover all datastores from vSphere.

    Args:
        content: vSphere ServiceInstanceContent.

    Returns:
        List of datastore info dicts with keys: name, id, type, capacity_gb, free_gb.
    """
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
    try:
        datastores = []
        for ds in container.view:  # type: ignore[attr-defined]
            summary = ds.summary
            datastores.append({
                "name": ds.name,
                "id": ds._moId,
                "type": summary.type or "unknown",
                "capacity_gb": round(summary.capacity / (1024**3), 1) if summary.capacity else 0,
                "free_gb": round(summary.freeSpace / (1024**3), 1) if summary.freeSpace else 0,
            })
        return datastores
    finally:
        container.Destroy()


def discover_vms(content: vim.ServiceInstanceContent) -> list[dict[str, Any]]:
    """Discover all VMs from vSphere using PropertyCollector for performance.

    Args:
        content: vSphere ServiceInstanceContent.

    Returns:
        List of VM info dicts with keys: name, power_state, guest_os.
    """
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        obj_spec = vim.PropertyCollector.ObjectSpec(  # type: ignore[attr-defined]
            obj=container,
            skip=True,
            selectSet=[
                vim.PropertyCollector.TraversalSpec(  # type: ignore[attr-defined]
                    type=vim.ContainerView,
                    path="view",
                    skip=False,
                )
            ],
        )
        prop_spec = vim.PropertyCollector.PropertySpec(  # type: ignore[attr-defined]
            type=vim.VirtualMachine,
            pathSet=["name", "config.template", "runtime.powerState", "config.guestFullName"],
        )
        filter_spec = vim.PropertyCollector.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])  # type: ignore[attr-defined]
        results = content.propertyCollector.RetrieveContents([filter_spec])

        vms = []
        for obj in results:
            props: dict[str, Any] = {}
            for prop in obj.propSet:
                props[prop.name] = prop.val

            if props.get("config.template", False):
                continue

            vms.append({
                "name": props.get("name", "unknown"),
                "power_state": str(props.get("runtime.powerState", "unknown")),
                "guest_os": props.get("config.guestFullName", "unknown") or "unknown",
            })
        return vms
    finally:
        container.Destroy()


def discover_esxi_hosts(content: vim.ServiceInstanceContent) -> list[dict[str, Any]]:
    """Discover all ESXi hosts from vSphere.

    Args:
        content: vSphere ServiceInstanceContent.

    Returns:
        List of host info dicts with keys: name, connection_state, power_state.
    """
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    try:
        hosts = []
        for host in container.view:  # type: ignore[attr-defined]
            hosts.append({
                "name": host.name,
                "connection_state": str(host.runtime.connectionState),
                "power_state": str(host.runtime.powerState),
            })
        return hosts
    finally:
        container.Destroy()


# ---------------------------------------------------------------------------
# OCP discovery
# ---------------------------------------------------------------------------


def connect_ocp(creds: dict[str, str]) -> DynamicClient:
    """Connect to OpenShift and return a DynamicClient.

    Args:
        creds: Credential dict from get_ocp_credentials().

    Returns:
        DynamicClient instance.

    Raises:
        ConnectionError: If connection fails.
    """
    verify_ssl = creds.get("verify_ssl", "true") == "true"
    ca_bundle = creds.get("ca_bundle", "")

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        console.print("  [dim]SSL certificate validation disabled for OCP connection[/dim]")

    with console.status("[bold]Connecting to OpenShift..."):
        try:
            if "kubeconfig" in creds:
                client = get_client()
            else:
                if verify_ssl and ca_bundle:
                    os.environ["KUBERNETES_CLIENT_CA_BUNDLE"] = ca_bundle

                client = get_client(
                    host=creds["host"],
                    username=creds["username"],
                    password=creds["password"],
                    verify_ssl=verify_ssl,
                )
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to OpenShift: {exc}") from exc
    console.print("  [green]Connected to OpenShift[/green]")
    return client


def discover_storage_classes(client: "DynamicClient") -> list[dict[str, str]]:
    """Discover storage classes from the OpenShift cluster.

    Args:
        client: DynamicClient instance.

    Returns:
        List of storage class info dicts with keys: name, provisioner, is_default.
    """
    classes = []
    for sc in StorageClass.get(client=client):
        annotations: dict[str, str] = sc.instance.metadata.get("annotations") or {}
        is_default = annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
        classes.append({
            "name": sc.name,
            "provisioner": sc.instance.provisioner or "unknown",
            "is_default": "Yes" if is_default else "",
        })
    return classes


def get_ocp_version(client: "DynamicClient") -> str:
    """Get the OpenShift cluster version string.

    Args:
        client: DynamicClient instance.

    Returns:
        Cluster version string (e.g. '4.16.3').

    Raises:
        ValueError: If version cannot be determined.
    """
    from ocp_resources.cluster_version import ClusterVersion  # noqa: PLC0415

    cluster_version = ClusterVersion(client=client, name="version", ensure_exists=True)
    try:
        return cluster_version.instance.status.desired.version
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"Failed to get OCP version: {exc}") from exc


def validate_mtv_installed(client: "DynamicClient") -> bool:
    """Check if MTV (ForkliftController) is installed on the cluster.

    Args:
        client: DynamicClient instance.

    Returns:
        True if MTV is installed, False if not found or API not available.
    """
    try:
        controllers = list(ForkliftController.get(client=client, namespace="openshift-mtv"))
        return len(controllers) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config building
# ---------------------------------------------------------------------------


def build_providers_json(
    vsphere_host: str,
    vsphere_user: str,
    vsphere_pass: str,
    version: str,
    guest_linux_user: str,
    guest_linux_pass: str,
    copyoffload_config: dict[str, Any],
    guest_vm_win_user: str = "",
    guest_vm_win_password: str = "",
) -> tuple[str, dict[str, Any]]:
    """Assemble the .providers.json structure for a copy-offload provider.

    Args:
        vsphere_host: vCenter hostname.
        vsphere_user: vSphere username.
        vsphere_pass: vSphere password.
        version: vSphere version string.
        guest_linux_user: Guest VM linux username.
        guest_linux_pass: Guest VM linux password.
        copyoffload_config: Copy-offload configuration dict.
        guest_vm_win_user: Guest VM Windows username (optional).
        guest_vm_win_password: Guest VM Windows password (optional).

    Returns:
        Tuple of (provider_key, provider_config_dict).
    """
    provider_key = f"vsphere-{version}"
    provider_config: dict[str, Any] = {
        "type": "vsphere",
        "version": version,
        "fqdn": vsphere_host,
        "api_url": f"https://{vsphere_host}/sdk",
        "username": vsphere_user,
        "password": vsphere_pass,
        "guest_vm_linux_user": guest_linux_user,
        "guest_vm_linux_password": guest_linux_pass,
        "copyoffload": copyoffload_config,
    }
    if guest_vm_win_user:
        provider_config["guest_vm_win_user"] = guest_vm_win_user
    if guest_vm_win_password:
        provider_config["guest_vm_win_password"] = guest_vm_win_password
    return provider_key, provider_config


def build_ocp_provider(
    ocp_creds: dict[str, str],
    ocp_version: str,
    storage_class: str,
) -> tuple[str, dict[str, Any]]:
    """Assemble the .providers.json structure for an OpenShift destination provider.

    Args:
        ocp_creds: OCP credential dict with 'host', 'username', 'password' keys.
        ocp_version: OpenShift cluster version string.
        storage_class: Selected storage class name.

    Returns:
        Tuple of (provider_key, provider_config_dict).

    Raises:
        ValueError: If ocp_creds is missing required keys (host, username, password).
    """
    for key in ("host", "username", "password"):
        if key not in ocp_creds:
            raise ValueError(
                f"OCP credentials missing required key '{key}'."
                " Kubeconfig-only auth requires host/username/password for provider config."
            )
    provider_key = f"openshift-{ocp_version}"
    host = ocp_creds["host"]
    provider_config: dict[str, Any] = {
        "type": "openshift",
        "version": ocp_version,
        "host": host,
        "api_url": host,
        "username": ocp_creds["username"],
        "password": ocp_creds["password"],
        "storage_class": storage_class,
    }
    if "verify_ssl" in ocp_creds:
        provider_config["verify_ssl"] = ocp_creds["verify_ssl"]
    if "ca_bundle" in ocp_creds:
        provider_config["ca_bundle"] = ocp_creds["ca_bundle"]
    return provider_key, provider_config


def mask_passwords(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of config with password values masked for display.

    Args:
        config: Configuration dict.

    Returns:
        Copy with passwords replaced by '***'.
    """
    masked: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            masked[key] = mask_passwords(value)
        elif "password" in key.lower() or "pwd" in key.lower():
            masked[key] = "***"
        else:
            masked[key] = value
    return masked


def get_providers_json_path() -> Path:
    """Resolve the providers JSON file path from env var or default.

    Uses ``PROVIDERS_JSON_PATH`` env var if set, otherwise falls back to
    ``DEFAULT_PROVIDERS_JSON_PATH``. Unlike ``resolve_providers_json_path()``,
    this does not validate existence (the file may not exist yet during generate).

    Returns:
        Path to the providers JSON file.
    """
    return Path(os.environ.get("PROVIDERS_JSON_PATH", DEFAULT_PROVIDERS_JSON_PATH))


def write_providers_json(provider_key: str, provider_config: dict[str, Any]) -> bool:
    """Write or merge provider config into providers JSON file.

    Args:
        provider_key: Provider key name.
        provider_config: Provider configuration dict.

    Returns:
        True if the file was written, False if the user declined overwrite.
    """
    providers_path = get_providers_json_path()
    existing: dict[str, Any] = {}
    if providers_path.exists():
        try:
            existing = json.loads(providers_path.read_text())
        except json.JSONDecodeError:
            console.print(f"  [yellow]Warning: existing {providers_path} is invalid, will overwrite[/yellow]")

    if provider_key in existing:
        if not Confirm.ask(f"  Provider '{provider_key}' already exists in {providers_path}. Overwrite?"):
            console.print(f"  [yellow]Skipping {providers_path} write[/yellow]")
            return False

    existing[provider_key] = provider_config

    _write_secret_file(providers_path, json.dumps(existing, indent=2) + "\n")
    console.print(f"  [green]Wrote {providers_path}[/green]")
    return True


def _write_secret_file(path: Path, content: str) -> None:
    """Write content to a file atomically with 0o600 permissions.

    Writes to a temporary file in the same directory (ensuring same filesystem),
    flushes and fsyncs the data, then atomically replaces the target via
    ``os.replace()``. This avoids leaving an empty or partial file if the
    process crashes mid-write.

    Args:
        path: File path to write.
        content: File content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd = None
    tmp_path: str | None = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
            tmp_fd = None  # fd is now managed by the file object
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, str(path))
        tmp_path = None
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def b64(value: str) -> str:
    """Base64 encode a string for Kubernetes Secret data.

    Args:
        value: String to encode.

    Returns:
        Base64 encoded string.
    """
    return base64.b64encode(value.encode()).decode()


def _build_job_secret_data(ocp_creds: dict[str, str], providers_json_content: str) -> str:
    """Build the Secret data section for the Job manifest.

    Args:
        ocp_creds: OCP credential dict with 'host', 'username', 'password' keys.
        providers_json_content: Raw JSON content of .providers.json.

    Returns:
        YAML string for the Secret data section.
    """
    verify_ssl = ocp_creds.get("verify_ssl", "true")
    ca_bundle = ocp_creds.get("ca_bundle", "")

    # ca_bundle may be a file path; read its contents so we encode the PEM data, not the path.
    ca_bundle_content = ""
    if ca_bundle:
        ca_path = Path(ca_bundle)
        if ca_path.exists():
            ca_bundle_content = ca_path.read_text()
        else:
            ca_bundle_content = ca_bundle  # already PEM content, not a path

    return (
        f"  providers.json: {b64(providers_json_content)}\n"
        f"  cluster_host: {b64(ocp_creds['host'])}\n"
        f"  cluster_username: {b64(ocp_creds['username'])}\n"
        f"  cluster_password: {b64(ocp_creds['password'])}\n"
        f"  cluster_verify_ssl: {b64(verify_ssl)}\n"
        f"  # TODO: ca_bundle is stored here but not yet consumed by the test runner container.\n"
        f"  # When the test runner supports CA bundle injection, mount this as a file and set\n"
        f"  # KUBERNETES_CLIENT_CA_BUNDLE or pass via --tc flag.\n"
        f"  cluster_ca_bundle: {b64(ca_bundle_content)}"
    )


def _build_pytest_command(
    marker_flag: str,
    filter_flag: str,
    provider_key: str,
    storage_class: str,
    insecure_verify_skip: str,
) -> str:
    """Build the pytest command string for the Job container.

    Args:
        marker_flag: Pytest marker flag (e.g. '-m tier0 ') or empty string.
        filter_flag: Pytest filter flag (e.g. "-k 'expr' ") or empty string.
        provider_key: Provider key from .providers.json.
        storage_class: OCP storage class name.
        insecure_verify_skip: Whether to skip SSL verification ('true' or 'false').

    Returns:
        Shell command string for pytest execution.
    """
    return (
        f"uv run pytest {marker_flag}{filter_flag}\\\n"
        f"              -v \\\n"
        f"              --tc=source_provider:{provider_key} \\\n"
        f"              --tc=storage_class:{storage_class} \\\n"
        f'              --tc=cluster_host:"$CLUSTER_HOST" \\\n'
        f"              --tc=insecure_verify_skip:{insecure_verify_skip}"
    )


def _build_job_spec(image: str, secret_name: str, namespace: str, job_name: str, pytest_command: str) -> str:
    """Build the Job spec YAML section.

    Args:
        image: Container image URL.
        secret_name: Name of the Kubernetes Secret.
        namespace: Kubernetes namespace for the Job.
        job_name: Name of the Job resource.
        pytest_command: Shell command to run in the container.

    Returns:
        YAML string for the Job resource.
    """
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {namespace}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
      - name: tests
        image: {image}
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop:
              - ALL
        env:
        - name: CLUSTER_HOST
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: cluster_host
        - name: CLUSTER_USERNAME
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: cluster_username
        - name: CLUSTER_PASSWORD
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: cluster_password
        - name: CLUSTER_VERIFY_SSL
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: cluster_verify_ssl
        command:
          - /bin/sh
          - -c
          - |
            {pytest_command}
        volumeMounts:
        - name: config
          mountPath: /app/.providers.json
          subPath: providers.json
      volumes:
      - name: config
        secret:
          secretName: {secret_name}
"""


def generate_job_yaml(
    provider_key: str,
    storage_class: str,
    category: str,
    image: str,
    ocp_creds: dict[str, str],
    providers_json_content: str,
    test_filter: str = "",
) -> tuple[str, str, str]:
    """Generate a self-contained YAML with Namespace + Secret + Job for MTV tests.

    The namespace name includes a short UUID suffix for isolation across
    concurrent runs.  The Secret contains providers.json and cluster
    credentials.  The Job mounts providers.json and passes cluster credentials
    via env vars and ``--tc=`` flags so that ``get_client(host, username,
    password)`` performs OAuth and sets a bearer token.

    Args:
        provider_key: Provider key from .providers.json.
        storage_class: OCP storage class name.
        category: Pytest marker name (e.g. 'copyoffload', 'tier0').
        image: Container image URL.
        ocp_creds: OCP credential dict with 'host', 'username', 'password' keys.
        providers_json_content: Raw JSON content of .providers.json.
        test_filter: Optional pytest -k filter expression.

    Returns:
        Tuple of (yaml_content, namespace, job_name).

    Raises:
        ValueError: If ocp_creds is missing required keys for Job generation.
    """
    required_keys = ("host", "username", "password")
    missing = [k for k in required_keys if k not in ocp_creds]
    if missing:
        raise ValueError(
            f"Job manifest requires OpenShift credentials ({', '.join(missing)} missing). "
            "Kubeconfig-only auth is not supported for Job generation."
        )

    image = image or DEFAULT_TEST_IMAGE
    namespace = _generate_namespace_name()
    secret_name = f"{namespace}-config"
    job_name = "mtv-tests" if category == "all" else f"mtv-{category}-tests"
    marker_flag = "" if category == "all" else f"-m {category} "
    filter_flag = f"-k {shlex.quote(test_filter)} " if test_filter else ""
    verify_ssl = ocp_creds.get("verify_ssl", "true")
    insecure_verify_skip = "false" if verify_ssl == "true" else "true"

    secret_data = _build_job_secret_data(ocp_creds=ocp_creds, providers_json_content=providers_json_content)
    pytest_command = _build_pytest_command(
        marker_flag=marker_flag,
        filter_flag=filter_flag,
        provider_key=provider_key,
        storage_class=storage_class,
        insecure_verify_skip=insecure_verify_skip,
    )
    job_spec = _build_job_spec(
        image=image,
        secret_name=secret_name,
        namespace=namespace,
        job_name=job_name,
        pytest_command=pytest_command,
    )

    yaml_content = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}

---

apiVersion: v1
kind: Secret
metadata:
  name: {secret_name}
  namespace: {namespace}
type: Opaque
data:
{secret_data}

---

{job_spec}"""
    return yaml_content, namespace, job_name
