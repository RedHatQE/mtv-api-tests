"""MTV API Tests CLI tool."""

from __future__ import annotations

from enum import StrEnum

import typer

from cli.mtv_api_tests.common import (
    DEFAULT_TEST_IMAGE,
    JOB_YAML_PATH,
    TEST_CATEGORIES,
    console,
)
from cli.mtv_api_tests.generate import generate_command
from cli.mtv_api_tests.measure_providers import measure_providers_command
from cli.mtv_api_tests.run import run_command

app = typer.Typer(help="MTV test setup and execution tool.", invoke_without_command=True, add_completion=False)


class RunMode(StrEnum):
    local = "local"
    job = "job"


@app.callback()
def main(ctx: typer.Context) -> None:
    """MTV test setup and execution tool.

    Args:
        ctx: Typer context with invoked subcommand info.
    """
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


@app.command()
def generate(
    image: str = typer.Option(DEFAULT_TEST_IMAGE, help="Container image for OCP Job"),  # noqa: M511
    category: str = typer.Option("", help=f"Test category ({', '.join(TEST_CATEGORIES)})"),  # noqa: M511
) -> None:
    """Interactive wizard to generate .providers.json and mtv-api-tests-manifests.yaml for MTV tests.

    Args:
        image: Container image for OCP Job.
        category: Pre-selected test category (empty for interactive).

    Raises:
        typer.Exit: On user abort or invalid input.
    """
    generate_command(image=image, category=category)


@app.command(name="measure-providers")
def measure_providers(
    providers_json: str = typer.Option("", help="Path to .providers.json (empty resolves the standard location)"),  # noqa: M511
) -> None:
    """Measure cluster-to-provider latency for every configured provider.

    Launches one probe pod on the OpenShift cluster, times TCP connects to every
    provider API endpoint, and prints a ranked table grouped by type and version.

    Args:
        providers_json: Path to `.providers.json` (empty resolves the standard
            location / `PROVIDERS_JSON_PATH` environment variable).

    Raises:
        typer.Exit: On missing providers file or probe failure.
    """
    measure_providers_command(providers_json=providers_json)


@app.command()
def run(
    mode: RunMode = typer.Option(..., help="Execution mode: 'local' (uv run pytest) or 'job' (oc apply)"),  # noqa: M511
    category: str = typer.Option("", help=f"Test category ({', '.join(TEST_CATEGORIES)})"),  # noqa: M511
    source_provider: str = typer.Option("", help="Source provider key from .providers.json"),  # noqa: M511
    destination_provider: str = typer.Option("", help="OCP destination provider key from .providers.json"),  # noqa: M511
    storage_class: str = typer.Option("", help="OpenShift storage class name"),  # noqa: M511
    test_filter: str = typer.Option("", "-k", help="Pytest -k filter (e.g. MTV-559, thin)"),  # noqa: M511
    job_yaml: str = typer.Option(str(JOB_YAML_PATH), help="Path to Job YAML (for --mode job)"),  # noqa: M511
) -> None:
    """Run tests locally or as an OpenShift Job.

    Args:
        mode: Execution mode ('local' or 'job').
        category: Pre-selected test category (empty for interactive).
        source_provider: Source provider key from .providers.json (empty for interactive).
        destination_provider: OCP destination provider key (empty for auto/interactive).
        storage_class: OpenShift storage class name (empty for interactive).
        test_filter: Pytest -k filter expression.
        job_yaml: Path to Job YAML file (for --mode job).

    Raises:
        typer.Exit: On test completion or error.
    """
    run_command(
        mode=mode.value,
        category=category,
        source_provider=source_provider,
        destination_provider=destination_provider,
        storage_class=storage_class,
        test_filter=test_filter,
        job_yaml=job_yaml,
    )
