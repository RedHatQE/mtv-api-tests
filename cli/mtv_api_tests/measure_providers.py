"""`measure-providers` CLI subcommand: cluster-to-provider latency report."""

from __future__ import annotations

import typer

from utilities.provider_latency import format_latency_table, probe_provider_latencies
from utilities.utils import get_cluster_client, load_source_providers


def measure_providers_command(providers_json: str) -> None:
    """Probe every configured provider from the cluster and print a ranked table.

    Args:
        providers_json (str): Path to `.providers.json` (empty resolves the standard
            location / `PROVIDERS_JSON_PATH` environment variable).

    Raises:
        typer.Exit: On missing providers file or probe failure.
    """
    providers = load_source_providers(providers_json_path=providers_json or None)
    rtts = probe_provider_latencies(client=get_cluster_client(), providers=providers)
    typer.echo(format_latency_table(providers=providers, rtts=rtts))
