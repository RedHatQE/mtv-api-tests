from typing import Any

import requests
import semver
import typer
from rich.console import Console


def main(version: str) -> None:
    """
    Get the latest MTV IIB for OCP 4/15/16/17

    Usage: uv run tools.mtv-iib <version> (only 2 digits version for example 2.8)
    """
    console = Console()
    iibs: dict[str, Any] = {}

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
        "v4.15": semver.Version.parse("0.0.0"),
        "v4.16": semver.Version.parse("0.0.0"),
        "v4.17": semver.Version.parse("0.0.0"),
    }

    for raw_msg in json_res["raw_messages"]:
        _index = raw_msg["msg"]["index"]
        _bundle_version = _index["added_bundle_images"][0].rsplit(":", 1)[-1]

        if version not in _bundle_version:
            continue

        semver_bundle = semver.Version.parse(_bundle_version)
        _ocp_version = _index["ocp_version"]
        _iib = _index["index_image"].rsplit(":", 1)[-1]

        if not iibs.get(_ocp_version):
            iibs[_ocp_version] = {"MTV": None, "IIB": None}

        if semver_bundle > mtv_latest[_ocp_version]:
            mtv_latest[_ocp_version] = semver_bundle
            iibs[_ocp_version]["MTV"] = _bundle_version
            iibs[_ocp_version]["IIB"] = _iib

    console.print(iibs)


if __name__ == "__main__":
    typer.run(main)
