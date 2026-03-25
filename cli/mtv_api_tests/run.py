"""Run subcommand — execute tests locally or as an OpenShift Job."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from rich.prompt import IntPrompt, Prompt

from cli.mtv_api_tests.common import (
    JOB_YAML_PATH,
    console,
    get_providers_json_path,
    prompt_test_filter,
    select_category,
)


def _resolve_ocp_provider(providers: dict[str, Any], destination_key: str) -> dict[str, Any] | None:
    """Resolve the OpenShift destination provider from user input or interactive selection.

    Args:
        providers: Loaded providers dict from .providers.json.
        destination_key: User-provided key (may be empty).

    Returns:
        The OpenShift provider config dict, or None if not found.

    Raises:
        typer.Exit: If key is invalid or selection fails.
    """
    ocp_providers = {k: v for k, v in providers.items() if v.get("type") == "openshift"}

    if destination_key:
        if destination_key not in ocp_providers:
            console.print(f"[red]Error: '{destination_key}' is not an OpenShift provider in .providers.json[/red]")
            raise typer.Exit(code=1)
        return ocp_providers[destination_key]

    if not ocp_providers:
        return None

    if len(ocp_providers) == 1:
        key, config = next(iter(ocp_providers.items()))
        console.print(f"  Using OCP provider: [bold]{key}[/bold]")
        return config

    console.print("\n[bold]Available OpenShift providers:[/bold]")
    keys = list(ocp_providers.keys())
    for idx, key in enumerate(keys, 1):
        version = ocp_providers[key].get("version", "?")
        console.print(f"  {idx}. {key} (v{version})")
    choice = IntPrompt.ask("  Select destination provider", default=1)
    if 1 <= choice <= len(keys):
        return ocp_providers[keys[choice - 1]]
    console.print("[red]Invalid choice[/red]")
    raise typer.Exit(code=1)


def _resolve_source_provider_key(providers: dict[str, Any], source_key: str) -> str:
    """Resolve source provider key from user input or interactive selection.

    Filters out non-source providers (e.g. openshift) from the selection list.

    Args:
        providers: Loaded providers dict from .providers.json.
        source_key: User-provided key (may be empty).

    Returns:
        Resolved provider key.

    Raises:
        typer.Exit: If key is invalid or selection fails.
    """
    source_providers = {k: v for k, v in providers.items() if v.get("type") != "openshift"}

    if not source_key:
        keys = list(source_providers.keys())
        if not keys:
            console.print("[red]Error: No source providers found in .providers.json[/red]")
            raise typer.Exit(code=1)
        if len(keys) == 1:
            source_key = keys[0]
            console.print(f"  Using source provider: [bold]{source_key}[/bold]")
        else:
            console.print("\n[bold]Available source providers:[/bold]")
            for idx, key in enumerate(keys, 1):
                console.print(f"  {idx}. {key}")
            choice = IntPrompt.ask("  Select source provider", default=1)
            if 1 <= choice <= len(keys):
                source_key = keys[choice - 1]
            else:
                console.print("[red]Invalid choice[/red]")
                raise typer.Exit(code=1)

    if source_key not in source_providers:
        console.print(f"[red]Error: Source provider '{source_key}' not found in .providers.json[/red]")
        raise typer.Exit(code=1)

    return source_key


def _get_cluster_config(ocp_provider: dict[str, Any] | None) -> tuple[list[str], dict[str, str]]:
    """Build cluster credential config for pytest from OCP provider config.

    Non-sensitive values (host, verify_ssl) are passed as --tc= args.
    Sensitive values (username, password) are passed as environment variables
    to avoid leaking credentials in /proc/cmdline.

    Args:
        ocp_provider: OpenShift provider config dict, or None.

    Returns:
        Tuple of (tc_args, env_vars) where tc_args is a list of --tc= arguments
        and env_vars is a dict of environment variables for the subprocess.
    """
    if ocp_provider:
        host = ocp_provider.get("host")
        username = ocp_provider.get("username")
        password = ocp_provider.get("password")
        if not all([host, username, password]):
            missing = [k for k in ("host", "username", "password") if not ocp_provider.get(k)]
            console.print(
                f"[red]Error: OCP provider is missing required fields: {', '.join(missing)}. "
                f"Update the provider in .providers.json or run 'generate' again.[/red]"
            )
            raise typer.Exit(code=1)
        else:
            console.print(f"  Using cluster credentials from OCP provider (v{ocp_provider.get('version', '?')})")
            tc_args = [f"--tc=cluster_host:{host}"]
            verify_ssl = ocp_provider.get("verify_ssl")
            if verify_ssl is not None:
                verify_ssl_bool = str(verify_ssl).lower() in ("true", "1", "yes")
                tc_args.append(f"--tc=insecure_verify_skip:{'false' if verify_ssl_bool else 'true'}")
            env_vars: dict[str, str] = {
                "CLUSTER_USERNAME": str(username),
                "CLUSTER_PASSWORD": str(password),
            }
            return tc_args, env_vars

    try:
        result = subprocess.run(
            ["oc", "whoami", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print("  [green]Using existing cluster token from 'oc whoami -t'[/green]")
            return [], {}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    console.print("\n[yellow]No cluster token found. Provide cluster credentials:[/yellow]")
    host = Prompt.ask("  Cluster API URL (e.g. https://api.cluster.com:6443)")
    username = Prompt.ask("  Cluster username", default="kubeadmin")
    password = Prompt.ask("  Cluster password", password=True)
    tc_args = [f"--tc=cluster_host:{host}"]
    prompt_env_vars: dict[str, str] = {
        "CLUSTER_USERNAME": username,
        "CLUSTER_PASSWORD": password,
    }
    return tc_args, prompt_env_vars


def _run_local(
    provider_key: str, storage_class: str, category: str, test_filter: str, ocp_provider: dict[str, Any] | None
) -> None:
    """Execute tests locally via uv run pytest.

    Args:
        provider_key: Source provider key from .providers.json.
        storage_class: OCP storage class name.
        category: Pytest marker name (e.g. 'copyoffload', 'tier0').
        test_filter: Optional pytest -k filter.
        ocp_provider: OpenShift provider config dict, or None.

    Raises:
        typer.Exit: With the pytest process return code.
    """
    cluster_args, cluster_env = _get_cluster_config(ocp_provider)

    cmd = [
        "uv",
        "run",
        "pytest",
        "-v",
        f"--tc=source_provider:{provider_key}",
        f"--tc=storage_class:{storage_class}",
        *cluster_args,
    ]
    if category != "all":
        cmd.extend(["-m", category])
    if test_filter:
        cmd.extend(["-k", test_filter])

    console.print(f"\n[bold]Running:[/bold] {shlex.join(cmd)}\n")

    # Merge cluster credentials into subprocess environment
    env = {**os.environ, **cluster_env} if cluster_env else None
    result = subprocess.run(cmd, env=env, check=False)
    raise typer.Exit(code=result.returncode)


def _run_job(job_yaml_path: Path) -> None:
    """Apply the Namespace, Secret, and Job from the manifest.

    Uses ``oc apply`` since each generated manifest has a unique namespace name,
    so there are no immutable-field conflicts.

    Args:
        job_yaml_path: Path to the Job YAML file.

    Raises:
        typer.Exit: If the apply command fails.
    """
    if not job_yaml_path.exists():
        console.print(f"[red]Error: {job_yaml_path} not found. Run 'generate' first.[/red]")
        raise typer.Exit(code=1)

    console.print("\n[bold]Deploying Namespace, Secret, and Job...[/bold]")
    result = subprocess.run(
        ["oc", "apply", "-f", str(job_yaml_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[red]Error: {result.stderr}[/red]")
        raise typer.Exit(code=1)

    # Check if resources were unchanged (Job won't re-run)
    combined_output = (result.stdout or "") + (result.stderr or "")
    if "unchanged" in combined_output:
        console.print(
            "[yellow]Warning: Job already exists and was not modified. "
            "A completed Job will not re-run.[/yellow]\n"
            f"  [dim]Delete first:[/dim] oc delete -f {job_yaml_path}"
        )
        raise typer.Exit(code=1)

    console.print("  [green]Deployed successfully[/green]")

    # Extract namespace and job name from the manifest for user guidance
    yaml_content = job_yaml_path.read_text()
    ns_match = re.search(r"kind: Namespace\nmetadata:\n  name: (\S+)", yaml_content)
    job_match = re.search(r"kind: Job\nmetadata:\n  name: (\S+)", yaml_content)
    if ns_match and job_match:
        namespace = ns_match.group(1)
        job_name = job_match.group(1)
        console.print(f"  [bold]Namespace:[/bold] {namespace}")
        console.print(f"\n  [dim]Follow logs:[/dim] oc logs -f -n {namespace} job/{job_name}")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def run_command(
    mode: str,
    category: str,
    source_provider: str,
    destination_provider: str,
    storage_class: str,
    test_filter: str,
    job_yaml: str,
) -> None:
    """Run tests locally or as an OpenShift Job.

    Args:
        mode: Execution mode ('local' or 'job').
        category: Pre-selected test category (empty for interactive).
        source_provider: Source provider key from .providers.json (empty for interactive).
        destination_provider: OCP destination provider key (empty for auto/interactive).
        storage_class: OpenShift storage class name (empty for interactive/auto from OCP provider).
        test_filter: Pytest -k filter expression.
        job_yaml: Path to Job YAML file.

    Raises:
        typer.Exit: On test completion, missing configuration, or error.
    """
    load_dotenv()

    if mode == "job":
        job_yaml_path = Path(job_yaml)
        if not job_yaml_path.exists():
            console.print(f"[red]Error: {job_yaml_path} not found. Run 'mtv-api-tests generate' first.[/red]")
            raise typer.Exit(code=1)
        _run_job(job_yaml_path)
        return

    providers_path = get_providers_json_path()
    if not providers_path.exists():
        console.print(f"[red]Error: {providers_path} not found. Run 'mtv-api-tests generate' first.[/red]")
        raise typer.Exit(code=1)

    try:
        providers = json.loads(providers_path.read_text())
    except json.JSONDecodeError as exc:
        console.print(f"[red]Error: {providers_path} contains invalid JSON: {exc}[/red]")
        raise typer.Exit(code=1)

    if not isinstance(providers, dict):
        console.print(f"[red]Error: {providers_path} must contain a JSON object, got {type(providers).__name__}[/red]")
        raise typer.Exit(code=1)

    invalid_provider_keys = [key for key, value in providers.items() if not isinstance(value, dict)]
    if invalid_provider_keys:
        invalid_keys = ", ".join(sorted(invalid_provider_keys))
        console.print(
            f"[red]Error: {providers_path} entries must be JSON objects. Invalid entries: {invalid_keys}[/red]"
        )
        raise typer.Exit(code=1)

    missing_type_keys = [key for key, value in providers.items() if isinstance(value, dict) and "type" not in value]
    if missing_type_keys:
        keys_str = ", ".join(sorted(missing_type_keys))
        console.print(f"[red]Error: {providers_path} entries must have a 'type' field. Missing in: {keys_str}[/red]")
        raise typer.Exit(code=1)

    resolved_category = select_category(category)

    test_filter = prompt_test_filter(test_filter)

    resolved_source = _resolve_source_provider_key(providers, source_provider)

    # Use OCP provider for storage class and cluster credentials
    ocp_provider = _resolve_ocp_provider(providers, destination_provider)
    if not storage_class:
        if ocp_provider and ocp_provider.get("storage_class"):
            storage_class = ocp_provider["storage_class"]
            console.print(f"  Using storage class: [bold]{storage_class}[/bold] (from OCP provider)")
        else:
            storage_class = Prompt.ask("  Enter storage class name")

    _run_local(resolved_source, storage_class, resolved_category, test_filter, ocp_provider)
