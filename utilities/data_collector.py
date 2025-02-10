import contextlib
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

import yaml
from kubernetes.dynamic import DynamicClient, ResourceInstance
from ocp_resources.datavolume import DataVolume
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume import PersistentVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import NamespacedResource, Resource
from ocp_resources.storage_map import StorageMap
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def prepare_base_path(base_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        # When running pytest in parallel (-n) we may get here error even when path exists
        if base_path.exists():
            shutil.rmtree(base_path)

    base_path.mkdir(parents=True, exist_ok=True)


def collect_pods_logs(logs_path: Path, namespace: str, client: DynamicClient, session_uuid: str) -> None:
    for _pod in Pod.get(namespace=namespace, dyn_client=client):
        if session_uuid in _pod.name:
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
    session_uuid: str,
) -> None:
    _resource_obj = resource(name=resource_instance.name, namespace=resource_instance.namespace, client=client)
    if _resource_obj.exists and session_uuid in _resource_obj.name:
        with open(yaml_path / f"{_resource_obj.name}.yaml", "w") as fd:
            yaml.dump(_resource_obj.instance.to_dict(), fd)


def collect_all_namespaced_resources_yaml(
    yaml_path: Path,
    resource: type[NamespacedResource],
    namespace: str,
    client: DynamicClient,
    session_uuid: str,
) -> None:
    for _resource in resource.get(namespace=namespace, dyn_client=client):
        if session_uuid in _resource.name:
            with open(yaml_path / f"{_resource.name}.yaml", "w") as fd:
                yaml.dump(_resource.instance.to_dict(), fd)


def collect_all_resources_yaml(
    yaml_path: Path, resource: type[Resource], client: DynamicClient, session_uuid: str
) -> None:
    for _resource in resource.get(dyn_client=client):
        if session_uuid in _resource.name:
            with open(yaml_path / f"{_resource.name}.yaml", "w") as fd:
                yaml.dump(_resource.instance.to_dict(), fd)


def data_collector(client: DynamicClient, base_path: Path, mtv_namespace: str, session_store: dict[str, Any]) -> None:
    LOGGER.info(f"Collecting logs in {base_path}")
    plans: list[Plan] = []
    session_uuid = session_store["session_uuid"]

    for plan in Plan.get(dyn_client=client, namespace=mtv_namespace):
        if session_uuid in plan.name:
            plans.append(plan)

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
        _target_ns_dv_path = Path(_target_namespace_path / "dv")
        _target_ns_pvc_path = Path(_target_namespace_path / "pvc")

        _pv_path = Path(base_path / "pv")

        for _path in (
            _network_map_path,
            _storage_map_path,
            _src_provider_path,
            _dst_provider_path,
            _mtv_pods_path,
            _target_ns_pods_path,
            _target_ns_dv_path,
            _target_ns_pvc_path,
            _pv_path,
        ):
            _path.mkdir(parents=True, exist_ok=True)

        # Collect plan.yaml
        with open(base_path / f"{_plan.name}-plan.yaml", "w") as fd:
            yaml.dump(_instance.to_dict(), fd)

        # Collect pods logs in mtv namespace and target namespace
        for ns, logs_path in zip([mtv_namespace, _target_namespace], [_mtv_pods_path, _target_ns_pods_path]):
            collect_pods_logs(logs_path=logs_path, namespace=ns, client=client, session_uuid=session_uuid)

        # Collect DVs in target namespace
        collect_all_namespaced_resources_yaml(
            yaml_path=_target_ns_dv_path,
            resource=DataVolume,
            namespace=_target_namespace,
            client=client,
            session_uuid=session_uuid,
        )

        # Collect PVCs in target namespace
        collect_all_namespaced_resources_yaml(
            yaml_path=_target_ns_pvc_path,
            resource=PersistentVolumeClaim,
            namespace=_target_namespace,
            client=client,
            session_uuid=session_uuid,
        )

        # Collect PVs
        collect_all_resources_yaml(
            yaml_path=_pv_path, resource=PersistentVolume, client=client, session_uuid=session_uuid
        )

        # Collect network map in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_network_map_path,
            resource=NetworkMap,
            resource_instance=_network_map,
            client=client,
            session_uuid=session_uuid,
        )

        # Collect storage map in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_storage_map_path,
            resource=StorageMap,
            resource_instance=_storage_map,
            client=client,
            session_uuid=session_uuid,
        )

        # Collect source provider in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_src_provider_path,
            resource=Provider,
            resource_instance=_src_provider,
            client=client,
            session_uuid=session_uuid,
        )

        # Collect destination provider in mtv namespace
        collect_namespaced_resource_yaml(
            yaml_path=_dst_provider_path,
            resource=Provider,
            resource_instance=_dst_provider,
            client=client,
            session_uuid=session_uuid,
        )


def zip_folder(folder_path, output_zip_path):
    # Ensure the folder path exists
    if not os.path.exists(folder_path):
        raise ValueError(f"Folder does not exist: {folder_path}")

    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(folder_path):
            for file in files:
                # Create the full path to the file
                file_path = os.path.join(root, file)
                # Add the file to the ZIP file
                zipf.write(file_path, arcname=os.path.relpath(file_path, start=folder_path))
