import importlib
import json
import sys


def clean_cluster_by_resources_file(resources_file: str) -> None:
    with open(resources_file, "r") as fd:
        data: dict[str, list[dict[str, str]]] = json.load(fd)

    for _, _resources_list in data.items():
        for _resource in _resources_list:
            _resource_module = importlib.import_module(_resource["module"])
            _resource_class = getattr(_resource_module, _resource["kind"])
            _kwargs = {"name": _resource["name"]}
            if _resource.get("namespace"):
                _kwargs["namespace"] = _resource["namespace"]

            _resource_class(**_kwargs).clean_up()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python clean_cluster.py <resources_file>")
        sys.exit(1)

    clean_cluster_by_resources_file(resources_file=sys.argv[1])
