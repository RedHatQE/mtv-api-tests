from __future__ import annotations
import shortuuid
import subprocess
from pathlib import Path
from typing import Any
import shutil

from ocp_resources.exceptions import MissingResourceResError
import pytest
from ocp_resources.hook import Hook
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.network_map import NetworkMap
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import DynamicClient, ResourceEditor, get_client
from ocp_resources.secret import Secret
from ocp_resources.storage_class import StorageClass
from ocp_resources.storage_map import StorageMap
from ocp_resources.namespace import Namespace
from ocp_resources.host import Host
from pytest_testconfig import py_config
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.virtual_machine import VirtualMachine
from libs.providers.cnv import CNVProvider
from utilities.utils import (
    create_source_cnv_vm,
    create_source_provider,
    gen_network_map_list,
    generate_name_with_uuid,
    is_true,
    start_source_vm_data_upload_vmware,
    vmware_provider,
    rhv_provider,
)
import logging
import os

from utilities.logger import separator, setup_logging

LOGGER = logging.getLogger(__name__)
BASIC_LOGGER = logging.getLogger("basic")


# Pytest start


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    # set a report attribute for each phase of a call, which can
    # be "setup", "call", "teardown"

    setattr(item, "rep_" + rep.when, rep)


def pytest_sessionstart(session):
    tests_log_file = session.config.getoption("log_file") or "pytest-tests.log"
    if os.path.exists(tests_log_file):
        Path(tests_log_file).unlink(missing_ok=True)

    session.config.option.log_listener = setup_logging(
        log_file=tests_log_file,
        log_level=session.config.getoption("log_cli_level") or logging.INFO,
    )


def pytest_fixture_setup(fixturedef, request):
    LOGGER.info(f"Executing {fixturedef.scope} fixture: {fixturedef.argname}")


def pytest_runtest_setup(item):
    BASIC_LOGGER.info(f"\n{separator(symbol_='-', val=item.name)}")
    BASIC_LOGGER.info(f"{separator(symbol_='-', val='SETUP')}")


def pytest_runtest_call(item):
    BASIC_LOGGER.info(f"{separator(symbol_='-', val='CALL')}")


def pytest_runtest_teardown(item):
    BASIC_LOGGER.info(f"{separator(symbol_='-', val='TEARDOWN')}")


def pytest_report_teststatus(report, config):
    test_name = report.head_line
    when = report.when
    call_str = "call"
    if report.passed:
        if when == call_str:
            BASIC_LOGGER.info(f"\nTEST: {test_name} STATUS: \033[0;32mPASSED\033[0m")

    elif report.skipped:
        BASIC_LOGGER.info(f"\nTEST: {test_name} STATUS: \033[1;33mSKIPPED\033[0m")

    elif report.failed:
        if when != call_str:
            BASIC_LOGGER.info(f"\nTEST: {test_name} [{when}] STATUS: \033[0;31mERROR\033[0m")
        else:
            BASIC_LOGGER.info(f"\nTEST: {test_name} STATUS: \033[0;31mFAILED\033[0m")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(path=session.config.option.basetemp, ignore_errors=True)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    reporter.summary_stats()


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        # Add test ID to test name
        item.name = f"{item.name}-{py_config.get('source_provider_type')}-{py_config.get('source_provider_version')}-{py_config.get('storage_class')}"


# Pytest end


@pytest.fixture(scope="session", autouse=True)
def autouse_fixtures(source_provider_data, nfs_storage_profile):
    # source_provider_data called here to fail fast in provider not found in the providers list from config
    yield


@pytest.fixture(scope="session")
def target_namespace(ocp_admin_client):
    """Delete and create the target namespace for MTV migrations"""
    namespaces: list[Namespace] = []
    label: dict[str, str] = {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
    }
    target_namespace: str = py_config["target_namespace"]

    # Generate a unique namespace name to avoid conflicts and support run multiple runs with the same provider configs
    unique_namespace_name = f"{target_namespace}-{shortuuid.uuid()}".lower()[:63]
    clients = [ocp_admin_client]
    if py_config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
        clients.append(ocp_admin_client)

    try:
        for client in clients:
            namespace = Namespace(client=client, name=unique_namespace_name, label=label)
            namespace.deploy(wait=True)
            namespaces.append(namespace)
            namespace.wait_for_status(status=namespace.Status.ACTIVE)
        yield unique_namespace_name

    finally:
        for namespace in namespaces:
            namespace.clean_up(wait=True)


@pytest.fixture(scope="session")
def nfs_storage_profile(ocp_admin_client):
    """
    Edit nfs StorageProfile CR with accessModes and volumeMode default settings
    More information: https://bugzilla.redhat.com/show_bug.cgi?id=2037652
    """
    nfs = StorageClass.Types.NFS
    if py_config["storage_class"] == nfs:
        storage_profile = StorageProfile(client=ocp_admin_client, name=nfs)
        if not storage_profile.exists:
            raise MissingResourceResError(f"StorageProfile {nfs} not found")

        with ResourceEditor(
            patches={
                storage_profile: {
                    "spec": {
                        "claimPropertySets": [
                            {
                                "accessModes": ["ReadWriteOnce"],
                                "volumeMode": "Filesystem",
                            }
                        ]
                    }
                }
            }
        ):
            yield

    else:
        yield


@pytest.fixture(scope="session")
def session_uuid():
    return generate_name_with_uuid(name="mtv-api-tests")


@pytest.fixture(scope="session")
def mtv_namespace():
    return py_config["mtv_namespace"]


@pytest.fixture(scope="session")
def ocp_admin_client(tmp_path_factory):
    """
    OCP client for remote cluster
    """

    if remote_cluster_name := py_config.get("remote_ocp_cluster"):
        LOGGER.info(msg=f"Creating remote OCP admin client for {remote_cluster_name}")

        mount_root = py_config.get("mount_root") or str(Path.home() / "cnv-qe.rhcloud.com")
        _remote_kubeconfig_path = f"{mount_root}/{remote_cluster_name}/auth/kubeconfig"

        if not Path(_remote_kubeconfig_path).exists():
            raise FileNotFoundError(f"Kubeconfig file {_remote_kubeconfig_path} not found")

        remote_kubeconfig_tmp_path = tmp_path_factory.mktemp("kubeconfig")
        remote_kubeconfig_tmp_file = Path(remote_kubeconfig_tmp_path / "kubeconfig")
        shutil.copyfile(_remote_kubeconfig_path, remote_kubeconfig_tmp_file)

        yield get_client(config_file=str(remote_kubeconfig_tmp_file))
    else:
        LOGGER.info(msg="Creating local OCP admin Client")
        yield get_client()


@pytest.fixture(scope="session")
def precopy_interval_forkliftcontroller(ocp_admin_client, mtv_namespace):
    """
    Set the snapshots interval in the forklift-controller ForkliftController
    """
    forklift_controller = ForkliftController(
        client=ocp_admin_client, name="forklift-controller", namespace=mtv_namespace
    )
    if not forklift_controller.exists:
        raise MissingResourceResError(f"ForkliftController {forklift_controller.name} not found")

    snapshots_interval = py_config["snapshots_interval"]
    forklift_controller.wait_for_condition(
        status=forklift_controller.Condition.Status.TRUE,
        condition=forklift_controller.Condition.Type.RUNNING,
        timeout=300,
    )

    LOGGER.info(
        f"Updating forklift-controller ForkliftController CR with snapshots interval={snapshots_interval} seconds"
    )

    with ResourceEditor(
        patches={
            forklift_controller: {
                "spec": {
                    "controller_precopy_interval": int(snapshots_interval),
                }
            }
        }
    ):
        forklift_controller.wait_for_condition(
            status=forklift_controller.Condition.Status.TRUE,
            condition=forklift_controller.Condition.Type.SUCCESSFUL,
            timeout=120,
        )

        yield


@pytest.fixture(scope="session")
def destination_provider(ocp_admin_client, mtv_namespace):
    provider = Provider(
        name=py_config.get("destination_provider_name", "host"), namespace=mtv_namespace, client=ocp_admin_client
    )
    if not provider.exists:
        raise MissingResourceResError(f"Provider {provider.name} not found")

    return CNVProvider(ocp_resource=provider)


@pytest.fixture(scope="session")
def source_provider_data():
    _source_provider_type = py_config["source_provider_type"]
    _source_provider_version = py_config["source_provider_version"]

    _source_provider = [
        _provider
        for _provider in py_config["source_providers_list"]
        if _provider["type"] == _source_provider_type
        and _provider["version"] == _source_provider_version
        and _provider["default"] == "True"
    ]

    if not _source_provider:
        raise ValueError(f"Source provider {_source_provider_type}-{_source_provider_version} not found")

    return _source_provider[0]


@pytest.fixture(scope="session")
def source_provider(source_provider_data, mtv_namespace, ocp_admin_client, tmp_path_factory):
    _teardown: list[Any] = []

    try:
        with create_source_provider(
            config=py_config,
            source_provider_data=source_provider_data,
            mtv_namespace=mtv_namespace,
            admin_client=ocp_admin_client,
            tmp_dir=tmp_path_factory,
        ) as source_provider_objects:
            _teardown.extend([src for src in source_provider_objects[1:] if src])
            source_provider_object = source_provider_objects[0]

        yield source_provider_object

    finally:
        for _resource in _teardown:
            if _resource:
                _resource.clean_up()

    source_provider_object.disconnect()


@pytest.fixture(scope="session")
def source_providers(mtv_namespace, ocp_admin_client, tmp_path_factory):
    _teardown: list[Any] = []
    try:
        for source_provider_data in py_config["source_providers_list"]:
            with create_source_provider(
                config=py_config,
                source_provider_data=source_provider_data,
                mtv_namespace=mtv_namespace,
                admin_client=ocp_admin_client,
                tmp_dir=tmp_path_factory,
            ) as source_provider_data:
                _teardown.extend([src for src in source_provider_data[1:] if src])
        yield

    finally:
        for _resource in _teardown:
            if _resource:
                _resource.clean_up()


@pytest.fixture(scope="session")
def source_provider_admin_user(source_provider_data, mtv_namespace, ocp_admin_client):
    if vmware_provider(provider_data=source_provider_data):
        _teardown: list[Any] = []
        try:
            with create_source_provider(
                config=py_config,
                source_provider_data=source_provider_data,
                mtv_namespace=mtv_namespace,
                admin_client=ocp_admin_client,
                username=source_provider_data["admin_username"],
                password=source_provider_data["admin_password"],
            ) as source_provider_object:
                _teardown.extend([src for src in source_provider_object[1:] if src])
                yield source_provider_object[0]

        finally:
            for _resource in _teardown:
                if _resource:
                    _resource.clean_up()
    else:
        yield


@pytest.fixture(scope="session")
def source_provider_non_admin_user(source_provider_data, mtv_namespace, ocp_admin_client):
    if vmware_provider(provider_data=source_provider_data):
        _teardown: list[Any] = []
        try:
            with create_source_provider(
                config=py_config,
                source_provider_data=source_provider_data,
                mtv_namespace=mtv_namespace,
                admin_client=ocp_admin_client,
                username=source_provider_data["non_admin_username"],
                password=source_provider_data["non_admin_password"],
            ) as source_provider_object:
                _teardown.extend([src for src in source_provider_object[1:] if src])
                yield source_provider_object[0]

        finally:
            for _resource in _teardown:
                if _resource:
                    _resource.clean_up()
    else:
        yield


@pytest.fixture(scope="session")
def multus_network_name(target_namespace, ocp_admin_client):
    nad_name: str = ""
    nads: list[NetworkAttachmentDefinition] = []
    clients: list[DynamicClient] = [ocp_admin_client]
    if py_config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
        clients.append(ocp_admin_client)

    try:
        for client in clients:
            nad = NetworkAttachmentDefinition(
                client=client,
                yaml_file="tests/manifests/second_network.yaml",
                namespace=target_namespace,
            )
            nad.deploy(wait=True)
            nad_name = nad.name
            nads.append(nad)

        yield nad_name
    finally:
        for _nad in nads:
            _nad.clean_up(wait=True)


@pytest.fixture(scope="session")
def network_migration_map_pod_only(
    source_provider, source_provider_data, destination_provider, mtv_namespace, ocp_admin_client, target_namespace
):
    network_map_list = gen_network_map_list(
        target_namespace=target_namespace, source_provider_data=source_provider_data, pod_only=True
    )
    with NetworkMap(
        client=ocp_admin_client,
        name=f"{source_provider.ocp_resources.name}-{destination_provider.ocp_resources.name}-network-map-pod",
        namespace=mtv_namespace,
        mapping=network_map_list,
        source_provider_name=source_provider.ocp_resources.name,
        source_provider_namespace=source_provider.ocp_resources.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    ) as network_map:
        yield network_map


@pytest.fixture(scope="session")
def network_migration_map(
    source_provider,
    source_provider_data,
    destination_provider,
    multus_network_name,
    mtv_namespace,
    ocp_admin_client,
    target_namespace,
):
    network_map_list = gen_network_map_list(
        target_namespace=target_namespace,
        source_provider_data=source_provider_data,
        multus_network_name=multus_network_name,
    )
    with NetworkMap(
        client=ocp_admin_client,
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-network-map",
        namespace=mtv_namespace,
        mapping=network_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    ) as network_map:
        yield network_map


@pytest.fixture(scope="session")
def storage_migration_map(source_provider, source_provider_data, destination_provider, mtv_namespace, ocp_admin_client):
    storage_map_list: list[dict[str, Any]] = []
    for storage in source_provider_data["storages"]:
        storage_map_list.append({
            "destination": {"storageClass": py_config["storage_class"]},
            "source": storage,
        })

    with StorageMap(
        client=ocp_admin_client,
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-{py_config['storage_class']}-storage-map",
        namespace=mtv_namespace,
        mapping=storage_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    ) as storage_map:
        yield storage_map


@pytest.fixture(scope="session")
def storage_migration_map_default_settings(
    source_provider, source_provider_data, destination_provider, mtv_namespace, ocp_admin_client
):
    storage_map_list: list[dict[str, Any]] = []
    for storage in source_provider_data["storages"]:
        storage_map_list.append({
            "destination": {
                "storageClass": py_config["storage_class"],
                "accessMode": "ReadWriteOnce",
                "volumeMode": "Filesystem",
            },
            "source": storage,
        })

    with StorageMap(
        client=ocp_admin_client,
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-{py_config['storage_class']}"
        f"-storage-map-default-settings",
        namespace=mtv_namespace,
        mapping=storage_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    ) as storage_map:
        yield storage_map


@pytest.fixture(scope="session")
def network_migration_map_source_admin(
    source_provider_admin_user,
    source_provider_data,
    destination_provider,
    multus_network_name,
    mtv_namespace,
    ocp_admin_client,
    target_namespace,
):
    if vmware_provider(provider_data=source_provider_data):
        network_map_list = gen_network_map_list(
            target_namespace=target_namespace,
            source_provider_data=source_provider_data,
            multus_network_name=multus_network_name,
        )
        with NetworkMap(
            client=ocp_admin_client,
            name=f"{source_provider_admin_user.ocp_resource.name}-{destination_provider.ocp_resource.name}-network-map",
            namespace=mtv_namespace,
            mapping=network_map_list,
            source_provider_name=source_provider_admin_user.ocp_resource.name,
            source_provider_namespace=source_provider_admin_user.ocp_resource.namespace,
            destination_provider_name=destination_provider.ocp_resource.name,
            destination_provider_namespace=destination_provider.ocp_resource.namespace,
        ) as network_map:
            yield network_map

    else:
        yield


@pytest.fixture(scope="session")
def storage_migration_map_source_admin(
    source_provider_admin_user,
    source_provider_data,
    destination_provider,
    mtv_namespace,
    ocp_admin_client,
):
    if vmware_provider(provider_data=source_provider_data):
        storage_map_list: list[dict[str, Any]] = []
        source_storages = source_provider_admin_user.storages_name
        for item in source_storages:
            storage_map_list.append({
                "destination": {"storageClass": py_config["storage_class"]},
                "source": {"name": item},
            })

        with StorageMap(
            client=ocp_admin_client,
            name=f"{source_provider_admin_user.ocp_resource.name}-{destination_provider.ocp_resource.name}-{py_config['storage_class']}"
            f"-storage-map",
            namespace=mtv_namespace,
            mapping=storage_map_list,
            source_provider_name=source_provider_admin_user.ocp_resource.name,
            source_provider_namespace=source_provider_admin_user.ocp_resource.namespace,
            destination_provider_name=destination_provider.ocp_resource.name,
            destination_provider_namespace=destination_provider.ocp_resource.namespace,
        ) as storage_map:
            yield storage_map

    else:
        yield


@pytest.fixture(scope="session")
def network_migration_map_source_non_admin(
    source_provider_non_admin_user,
    source_provider_data,
    destination_provider,
    multus_network_name,
    mtv_namespace,
    ocp_admin_client,
    target_namespace,
):
    if vmware_provider(provider_data=source_provider_data):
        network_map_list = gen_network_map_list(
            target_namespace=target_namespace,
            source_provider_data=source_provider_data,
            multus_network_name=multus_network_name,
        )
        with NetworkMap(
            client=ocp_admin_client,
            name=f"{source_provider_non_admin_user.ocp_resource.name}-{destination_provider.ocp_resource.name}-network-map",
            namespace=mtv_namespace,
            mapping=network_map_list,
            source_provider_name=source_provider_non_admin_user.ocp_resource.name,
            source_provider_namespace=source_provider_non_admin_user.ocp_resource.namespace,
            destination_provider_name=destination_provider.ocp_resource.name,
            destination_provider_namespace=destination_provider.ocp_resource.namespace,
        ) as network_map:
            yield network_map

    else:
        yield


@pytest.fixture(scope="session")
def storage_migration_map_source_non_admin(
    source_provider_non_admin_user,
    source_provider_data,
    destination_provider,
    mtv_namespace,
    ocp_admin_client,
):
    if vmware_provider(provider_data=source_provider_data):
        storage_map_list: list[dict[str, Any]] = []
        source_storages = source_provider_non_admin_user.storages_name
        for item in source_storages:
            storage_map_list.append({
                "destination": {"storageClass": py_config["storage_class"]},
                "source": {"name": item},
            })

        with StorageMap(
            client=ocp_admin_client,
            name=f"{source_provider_non_admin_user.ocp_resource.name}-{destination_provider.ocp_resource.name}-{py_config['storage_class']}"
            f"-storage-map",
            namespace=mtv_namespace,
            mapping=storage_map_list,
            source_provider_name=source_provider_non_admin_user.ocp_resource.name,
            source_provider_namespace=source_provider_non_admin_user.ocp_resource.namespace,
            destination_provider_name=destination_provider.ocp_resource.name,
            destination_provider_namespace=destination_provider.ocp_resource.namespace,
        ) as storage_map:
            yield storage_map

    else:
        yield


@pytest.fixture(scope="session")
def plans_scale(source_provider):
    source_vms = source_provider.vms(search=py_config["vm_name_search_pattern"])
    plans: list[dict[str, Any]] = [
        {
            "virtual_machines": [],
            "warm_migration": py_config["warm_migration"],
        }
    ]

    for idx in range(int(py_config["number_of_vms"])):
        vm_name = source_vms[idx].name
        plans[0]["virtual_machines"].append({"name": f"{vm_name}"})

        if is_true(py_config.get("turn_on_vms")):
            source_vm_details = source_provider.vm_dict(name=vm_name)
            source_provider.start_vm(vm=source_vm_details["provider_vm_api"])

    return plans


@pytest.fixture(scope="session")
def destination_ocp_secret(ocp_admin_client, session_uuid, mtv_namespace):
    api_key: str = ocp_admin_client.configuration.api_key.get("authorization")
    if not api_key:
        raise ValueError("API key not found in configuration, please login with `oc login` first")

    with Secret(
        name=f"{session_uuid}-ocp-secret",
        namespace=mtv_namespace,
        # API key format: 'Bearer sha256~<token>', split it to get token.
        string_data={"token": api_key.split()[-1], "insecureSkipVerify": "true"},
    ) as secret:
        yield secret


@pytest.fixture(scope="session")
def destination_ocp_provider(destination_ocp_secret, ocp_admin_client, session_uuid, mtv_namespace):
    provider_name: str = f"{session_uuid}-ocp-provider"
    with Provider(
        name=provider_name,
        namespace=mtv_namespace,
        secret_name=destination_ocp_secret.name,
        secret_namespace=destination_ocp_secret.namespace,
        url=ocp_admin_client.configuration.host,
        provider_type=Provider.ProviderType.OPENSHIFT,
    ) as ocp_resource_provider:
        yield CNVProvider(ocp_resource=ocp_resource_provider)


@pytest.fixture(scope="session")
def remote_network_migration_map(
    source_provider,
    source_provider_data,
    destination_ocp_provider,
    session_uuid,
    multus_network_name,
    mtv_namespace,
    target_namespace,
):
    network_map_list = gen_network_map_list(
        target_namespace=target_namespace,
        source_provider_data=source_provider_data,
        multus_network_name=multus_network_name,
    )
    with NetworkMap(
        name=f"{session_uuid}-networkmap",
        namespace=mtv_namespace,
        mapping=network_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_ocp_provider.ocp_resource.name,
        destination_provider_namespace=destination_ocp_provider.ocp_resource.namespace,
    ) as network_map:
        yield network_map


@pytest.fixture(scope="session")
def remote_storage_migration_map(
    source_provider, source_provider_data, destination_ocp_provider, session_uuid, mtv_namespace, ocp_admin_client
):
    storage_map_list: list[dict[str, Any]] = []
    for storage in source_provider_data["storages"]:
        if py_config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
            storage_class = StorageClass(name=storage["name"], client=ocp_admin_client)
            storage.update({"id": storage_class.instance.metadata.uid})

        storage_map_list.append({
            "destination": {"storageClass": py_config["storage_class"]},
            "source": storage,
        })

    with StorageMap(
        name=f"{session_uuid}-storagemap",
        namespace=mtv_namespace,
        mapping=storage_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_ocp_provider.ocp_resource.name,
        destination_provider_namespace=destination_ocp_provider.ocp_resource.namespace,
    ) as storage_map:
        yield storage_map


@pytest.fixture(scope="session")
def plans_set():
    plans: list[dict[str, Any]] = [
        {
            "virtual_machines": [],
            "warm_migration": py_config["warm_migration"],
        }
    ]

    for vm_name in py_config["list_of_vms_csv"].split(","):
        plans[0]["virtual_machines"].append({"name": vm_name})

    return plans


@pytest.fixture(scope="session")
def source_provider_host_secret(source_provider, source_provider_data, mtv_namespace, ocp_admin_client):
    if source_provider_data.get("host_list"):
        host = source_provider_data["host_list"][0]
        name = f"{source_provider_data['fqdn']}-{host['migration_host_ip']}-{host['migration_host_id']}"
        string_data: dict[str, str] = {
            "user": host["user"],
            "password": host["password"],
        }
        with Secret(
            client=ocp_admin_client, name=name.replace(".", "-"), namespace=mtv_namespace, string_data=string_data
        ) as secret:
            yield secret
    else:
        yield


@pytest.fixture(scope="session")
def source_provider_host(
    source_provider, source_provider_data, mtv_namespace, source_provider_host_secret, ocp_admin_client
):
    if source_provider_data.get("host_list"):
        host = source_provider_data["host_list"][0]
        with Host(
            client=ocp_admin_client,
            name=f"{source_provider_data['fqdn']}-{host['migration_host_ip']}-{host['migration_host_id']}",
            namespace=mtv_namespace,
            ip_address=host["migration_host_ip"],
            host_id=host["migration_host_id"],
            provider_name=source_provider.ocp_resource.name,
            provider_namespace=source_provider.ocp_resource.namespace,
            secret_name=source_provider_host_secret.name,
            secret_namespace=source_provider_host_secret.namespace,
        ) as host:
            yield host

    else:
        yield


@pytest.fixture(scope="session")
def prehook(ocp_admin_client, mtv_namespace):
    pre_hook_dict: dict[str, str] = py_config["hook_dict"]["prehook"]
    with Hook(
        client=ocp_admin_client,
        name=pre_hook_dict["name"],
        namespace=mtv_namespace,
        playbook=pre_hook_dict["payload"],
    ) as hook:
        yield hook


@pytest.fixture(scope="session")
def posthook(ocp_admin_client, mtv_namespace):
    posthook_dict: dict[str, str] = py_config["hook_dict"]["posthook"]
    with Hook(
        client=ocp_admin_client,
        name=posthook_dict["name"],
        namespace=mtv_namespace,
        playbook=posthook_dict["payload"],
    ) as hook:
        yield hook


@pytest.fixture(scope="function")
def skip_if_no_vmware(source_provider_data):
    if not vmware_provider(provider_data=source_provider_data):
        pytest.skip("Skip testing. VMware only test.")


@pytest.fixture(scope="function")
def skip_if_no_rhv(source_provider_data):
    if not rhv_provider(provider_data=source_provider_data):
        pytest.skip("Skip testing. RHV only test.")


@pytest.fixture(scope="function")
def plans(target_namespace, ocp_admin_client, source_provider_data, source_provider, request):
    plan: dict[str, Any] = request.param[0]
    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
    vm_names_list: list[str] = [vm["name"] for vm in virtual_machines]

    if py_config["source_provider_type"] != Provider.ProviderType.OVA:
        openshift_source_provider: bool = py_config["source_provider_type"] == Provider.ProviderType.OPENSHIFT

        for vm in virtual_machines:
            if openshift_source_provider:
                create_source_cnv_vm(ocp_admin_client, vm["name"], namespace=target_namespace)

            source_vm_details = source_provider.vm_dict(name=vm["name"], namespace=target_namespace, source=True)
            provider_vm_api = source_vm_details["provider_vm_api"]

            vm["snapshots_before_migration"] = source_vm_details["snapshots_data"]
            if vm.get("source_vm_power") == "on":
                source_provider.start_vm(provider_vm_api)

            elif vm.get("source_vm_power") == "off":
                if openshift_source_provider:
                    source_provider.stop_vm(provider_vm_api)
                else:
                    source_provider.power_off_vm(provider_vm_api)

    # Uploading Data to the source guest vm that may be validated later
    # The source VM is required to be running
    # Once there are no more running VMs the thread is terminated.
    # skip if pre_copies_before_cut_over is not set
    if (
        plan.get("warm_migration")
        and len([
            vm
            for vm in virtual_machines
            # Start Working only if all vms are expected to be turned on.
            if vm.get("source_vm_power") == "on"
        ])
        == len(virtual_machines)
        and plan.get("pre_copies_before_cut_over")
    ):
        LOGGER.info("Starting Data Upload to source VMs")
        start_source_vm_data_upload_vmware(provider_data=source_provider_data, vm_names_list=vm_names_list)

    yield request.param

    # Cleanup target resources of a positive test that succeeded
    # Getting the @pytest.marks of the test
    test_marks = [_mark.name for _mark in request.node.iter_markers()]
    if (
        is_true(py_config.get("clean_target_resources", False))
        and request.node.rep_call.passed
        and "negative" not in test_marks
    ):
        for vm in virtual_machines:
            vm = VirtualMachine(
                client=ocp_admin_client,
                name=vm["name"],
                namespace=target_namespace,
            )
            if vm.exists:
                vm.delete(wait=True)

        for pod in Pod.get(client=ocp_admin_client, namespace=target_namespace):
            if plan["name"] in pod.name:
                if pod.exists:
                    pod.delete(wait=True)


@pytest.fixture(scope="function")
def restore_ingress_certificate():
    yield
    assert subprocess.run(["/bin/sh", "./utilities/publish.sh", "restore"]).returncode == 0, (
        "external certification restore check"
    )
