from typing import OrderedDict

import requests
import semver
import typer
from rich.console import Console
from rich.tree import Tree


def get_mtv_latest_iib(version: str) -> dict[str, dict[str, str]]:
    """
    Get the latest MTV IIB for OCP versions

    Usage: uv run tools.mtv-iib <version> (only 2 digits version for example 2.8)
    """

    iibs: dict[str, dict[str, str]] = OrderedDict({
        "v4.16": {},
        "v4.17": {},
        "v4.18": {},
        "v4.19": {},
        "v4.20": {},
    })

    datagrepper_query_url = (
        "https://datagrepper.engineering.redhat.com/raw?topic=/topic/"
        "VirtualTopic.eng.ci.redhat-container-image.index.built"
    )

    res = requests.get(
        f"{datagrepper_query_url}&contains=mtv-operator-bundle-container",
        verify=False,
    )

    json_res = res.json()
    mtv_latest: dict[str, semver.Version] = {
        "v4.16": semver.Version.parse("0.0.0"),
        "v4.17": semver.Version.parse("0.0.0"),
        "v4.18": semver.Version.parse("0.0.0"),
        "v4.19": semver.Version.parse("0.0.0"),
        "v4.20": semver.Version.parse("0.0.0"),
    }

    for raw_msg in json_res["raw_messages"]:
        _index = raw_msg["msg"]["index"]
        _bundle_version = _index["added_bundle_images"][0].rsplit(":", 1)[-1]

        if version not in _bundle_version:
            continue

        semver_bundle = semver.Version.parse(_bundle_version)
        _ocp_version = _index["ocp_version"]
        _iib = _index["index_image"].rsplit(":", 1)[-1]

        if semver_bundle > mtv_latest[_ocp_version]:
            mtv_latest[_ocp_version] = semver_bundle
            iibs[_ocp_version] = {"MTV": _bundle_version, "IIB": _iib}

    return iibs


def main(version: str) -> None:
    tree = Tree("MTV IIBs")
    console = Console()

    for ocp_version, iib in get_mtv_latest_iib(version=version).items():
        if not iib:
            continue

        tree.add(f"{ocp_version} {iib['MTV']} {iib['IIB']}")

    console.print(tree)


if __name__ == "__main__":
    typer.run(main)
