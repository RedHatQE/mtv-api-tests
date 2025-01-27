import shutil
from pathlib import Path

import yaml
from kubernetes.dynamic import DynamicClient, ResourceInstance
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume import PersistentVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import NamespacedResource, Resource, get_client
from ocp_resources.storage_map import StorageMap
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def prepare_base_path(base_path: Path) -> None:
    if base_path.exists():
        shutil.rmtree(base_path)

    base_path.mkdir(parents=True, exist_ok=True)


def collect_pods_logs(logs_path: Path, namespace: str, client: DynamicClient) -> None:
    for _pod in Pod.get(namespace=namespace, dyn_client=client):
        try:
            for _container in _pod.instance.spec.containers:
                _container_name = _container["name"]
                with open(logs_path / f"{_pod.name}-{_container_name}.log", "w") as fd:
                    fd.write(_pod.log(container=_container_name))
        except Exception:
            LOGGER.warning(f"Failed to collect logs for pod {_pod.name}")


def collect_namespaced_resource_yaml(
    yaml_path: Path,
    resource: type[NamespacedResource],
    resource_instance: ResourceInstance,
    client: DynamicClient,
) -> None:
    _resource_obj = resource(name=resource_instance.name, namespace=resource_instance.namespace, client=client)
    if _resource_obj.exists:
        with open(yaml_path / f"{_resource_obj.name}.yaml", "w") as fd:
            yaml.dump(_resource_obj.instance.to_dict(), fd)


def collect_all_namespaced_resources_yaml(
    yaml_path: Path, resource: type[NamespacedResource], namespace: str, client: DynamicClient
) -> None:
    for _resource in resource.get(namespace=namespace, dyn_client=client):
        with open(yaml_path / f"{_resource.name}.yaml", "w") as fd:
            yaml.dump(_resource.instance.to_dict(), fd)


def collect_all_resources_yaml(yaml_path: Path, resource: type[Resource], client: DynamicClient) -> None:
    for _resource in resource.get(dyn_client=client):
        with open(yaml_path / f"{_resource.name}.yaml", "w") as fd:
            yaml.dump(_resource.instance.to_dict(), fd)


def data_collector(client: DynamicClient, base_path: Path, mtv_namespace: str, plan: Plan | None = None) -> None:
    LOGGER.info(f"Collecting logs in {base_path}")
    plans = [plan] if plan else Plan.get(dyn_client=client, namespace=mtv_namespace)

    for _plan in plans:
        _instance = _plan.instance
        _target_namespace = _instance.spec.targetNamespace
        _network_map = _instance.spec.map.network
        _storage_map = _instance.spec.map.storage
        _dst_provider = _instance.spec.provider.destination
        _src_provider = _instance.spec.provider.source

        _mtv_namespace_path = Path(base_path / mtv_namespace)
        _target_namespace_path = Path(base_path / _target_namespace)

        _network_map_path = Path(_mtv_namespace_path / "network_map")
        _storage_map_path = Path(_mtv_namespace_path / "storage_map")
        _src_provider_path = Path(_mtv_namespace_path / "source_provider")
        _dst_provider_path = Path(_mtv_namespace_path / "destination_provider")
        _mtv_pods_path = Path(_mtv_namespace_path / "pods")

        _target_ns_pods_path = Path(_target_namespace_path / "pods")
        _target_ns_pvc_path = Path(_target_namespace_path / "pvc")

        _pv_path = Path(base_path / "pv")

        for _path in (
            _network_map_path,
            _storage_map_path,
            _src_provider_path,
            _dst_provider_path,
            _mtv_pods_path,
            _target_ns_pods_path,
            _target_ns_pvc_path,
            _pv_path,
        ):
            _path.mkdir(parents=True, exist_ok=True)

        # Collect plan.yaml
        with open(base_path / f"{_plan.name}-plan.yaml", "w") as fd:
            yaml.dump(_instance.to_dict(), fd)

        # Collect pods logs in mtv namespace and target namespace
        for ns, logs_path in zip([mtv_namespace, _target_namespace], [_mtv_pods_path, _target_ns_pods_path]):
            collect_pods_logs(logs_path=logs_path, namespace=ns, client=client)

        # Collect PVCs in target namespace
        collect_all_namespaced_resources_yaml(
            yaml_path=_target_ns_pvc_path,
            resource=PersistentVolumeClaim,
            namespace=_target_namespace,
            client=client,
        )

        # Collect PVs
        collect_all_resources_yaml(yaml_path=_pv_path, resource=PersistentVolume, client=client)

        # Collect network map in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_network_map_path,
            resource=NetworkMap,
            resource_instance=_network_map,
            client=client,
        )

        # Collect storage map in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_storage_map_path,
            resource=StorageMap,
            resource_instance=_storage_map,
            client=client,
        )

        # Collect source provider in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_src_provider_path,
            resource=Provider,
            resource_instance=_src_provider,
            client=client,
        )

        # Collect destination provider in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_dst_provider_path,
            resource=Provider,
            resource_instance=_dst_provider,
            client=client,
        )


if __name__ == "__main__":
    import sys

    try:
        plan_name = sys.argv[1]
    except IndexError:
        print("Usage: python data_collector.py <plan_name>")
        sys.exit(1)

    mtv_namespace = "openshift-mtv"
    logs_path = Path(f".local/plan-{plan_name}-debug")
    plan = Plan(name=plan_name, namespace=mtv_namespace)
    prepare_base_path(base_path=logs_path)
    data_collector(client=get_client(), base_path=logs_path, mtv_namespace=mtv_namespace, plan=plan)
