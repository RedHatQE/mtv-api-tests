from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.exceptions import MissingResourceResError
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.hook import Hook
from ocp_resources.host import Host
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.network_map import NetworkMap
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor, get_client
from ocp_resources.secret import Secret
from ocp_resources.storage_class import StorageClass
from ocp_resources.storage_map import StorageMap
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.virtual_machine import VirtualMachine
from pytest_harvest import get_fixture_store
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutSampler

from libs.providers.cnv import CNVProvider
from utilities.logger import separator, setup_logging
from utilities.must_gather import run_must_gather
from utilities.pytest_utils import SessionTeardownError, collect_created_resources, prepare_base_path, session_teardown
from utilities.resources import create_and_store_resource
from utilities.utils import (
    create_source_cnv_vm,
    create_source_provider,
    gen_network_map_list,
    generate_name_with_uuid,
    get_source_provider_data,
    get_value_from_py_config,
    start_source_vm_data_upload_vmware,
)

LOGGER = logging.getLogger(__name__)
BASIC_LOGGER = logging.getLogger("basic")


class RemoteClusterAndLocalCluterNamesError(Exception):
    pass


class ForkliftPodsNotRunningError(Exception):
    pass


# Pytest start


def pytest_addoption(parser):
    data_collector_group = parser.getgroup(name="DataCollector")
    teardown_group = parser.getgroup(name="Teardown")
    data_collector_group.addoption("--skip-data-collector", action="store_true", help="Collect data for failed tests")
    data_collector_group.addoption(
        "--data-collector-path", help="Path to store collected data for failed tests", default=".data-collector"
    )
    teardown_group.addoption(
        "--skip-teardown", action="store_true", help="Do not teardown resource created by the tests"
    )


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    # set a report attribute for each phase of a call, which can
    # be "setup", "call", "teardown"

    setattr(item, "rep_" + rep.when, rep)


def pytest_sessionstart(session):
    _session_store = get_fixture_store(session)
    _session_store["teardown"] = {}

    if not session.config.getoption("skip_data_collector"):
        _data_collector_path = Path(session.config.getoption("data_collector_path"))
        prepare_base_path(base_path=_data_collector_path)

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
    if session.config.option.setupplan or session.config.option.collectonly:
        return

    _session_store = get_fixture_store(session)

    if not session.config.getoption("skip_data_collector"):
        _data_collector_path = Path(session.config.getoption("data_collector_path"))
        collect_created_resources(session_store=_session_store, data_collector_path=_data_collector_path)

    if session.config.getoption("skip_teardown"):
        LOGGER.warning("User requested to skip teardown of resources")

    else:
        # TODO: Maybe we need to check session_teardown return and fail the run if any leftovers
        try:
            session_teardown(session_store=_session_store)
        except SessionTeardownError:
            run_must_gather(data_collector_path=session.config.getoption("data_collector_path"))

    shutil.rmtree(path=session.config.option.basetemp, ignore_errors=True)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    reporter.summary_stats()


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        # Add test ID to test name
        item.name = f"{item.name}-{py_config.get('source_provider_type')}-{py_config.get('source_provider_version')}-{py_config.get('storage_class')}"


def pytest_exception_interact(node, call, report):
    if not node.session.config.getoption("skip_data_collector"):
        _session_store = get_fixture_store(node.session)
        _data_collector_path = Path(f"{node.session.config.getoption('data_collector_path')}/{node.name}")
        plans = _session_store.get(node.name, {}).get("plans", [])
        run_must_gather(data_collector_path=_data_collector_path, plans=plans)


# Pytest end


@pytest.fixture(scope="session", autouse=True)
def autouse_fixtures(source_provider_data, nfs_storage_profile, forklift_pods_state):
    # source_provider_data called here to fail fast in provider not found in the providers list from config
    yield


@pytest.fixture(scope="session")
def target_namespace(fixture_store, session_uuid, ocp_admin_client):
    """create the target namespace for MTV migrations"""
    label: dict[str, str] = {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
    }
    _target_namespace: str = py_config["target_namespace"]

    # replace mtv-api-tests since session_uuid already include mtv-api-tests in the name
    _target_namespace = _target_namespace.replace("mtv-api-tests", "")

    # Generate a unique namespace name to avoid conflicts and support run multiple runs with the same provider configs
    unique_namespace_name = f"{session_uuid}{_target_namespace}"[:63]
    fixture_store["target_namespace"] = unique_namespace_name

    namespace = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Namespace,
        client=ocp_admin_client,
        name=unique_namespace_name,
        label=label,
    )
    namespace.wait_for_status(status=namespace.Status.ACTIVE)
    yield namespace.name


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
def session_uuid(fixture_store):
    _session_uuid = generate_name_with_uuid(name="mtv-api-tests")
    fixture_store["session_uuid"] = _session_uuid
    return _session_uuid


@pytest.fixture(scope="session")
def mtv_namespace():
    return py_config["mtv_namespace"]


@pytest.fixture(scope="session")
def ocp_admin_client():
    """
    OCP client
    """
    LOGGER.info(msg="Creating OCP admin Client")
    _client = get_client()

    if remote_cluster_name := get_value_from_py_config("remote_ocp_cluster"):
        if remote_cluster_name not in _client.configuration.host:
            raise RemoteClusterAndLocalCluterNamesError("Remote cluster must be the same as local cluster.")

    yield _client


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
            timeout=300,
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
    _source_provider = get_source_provider_data()

    if not _source_provider:
        raise ValueError(f"Source provider {_source_provider['type']}-{_source_provider['version']} not found")

    return _source_provider


@pytest.fixture(scope="session")
def source_provider(
    fixture_store, session_uuid, source_provider_data, mtv_namespace, ocp_admin_client, tmp_path_factory
):
    with create_source_provider(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        config=py_config,
        source_provider_data=source_provider_data,
        mtv_namespace=mtv_namespace,
        admin_client=ocp_admin_client,
        tmp_dir=tmp_path_factory,
    ) as source_provider_objects:
        _source_provider = source_provider_objects[0]

        yield _source_provider

    _source_provider.disconnect()


@pytest.fixture(scope="session")
def multus_network_name(fixture_store, session_uuid, target_namespace, ocp_admin_client):
    nad_name: str = ""
    clients: list[DynamicClient] = [ocp_admin_client]

    if py_config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
        clients.append(ocp_admin_client)

    with open("tests/manifests/second_network.yaml") as fd:
        bridge_yaml = yaml.safe_load(fd)

    bridge_name = bridge_yaml["metadata"]["name"]
    bridge_yaml["metadata"]["name"] = f"{session_uuid}-{bridge_name}"

    for client in clients:
        nad = create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=NetworkAttachmentDefinition,
            client=client,
            kind_dict=bridge_yaml,
            namespace=target_namespace,
        )
        nad_name = nad.name

    yield nad_name


@pytest.fixture(scope="session")
def network_migration_map(
    fixture_store,
    session_uuid,
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
    network_map = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=NetworkMap,
        client=ocp_admin_client,
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-network-map",
        namespace=mtv_namespace,
        mapping=network_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    )
    yield network_map


@pytest.fixture(scope="session")
def storage_migration_map(
    fixture_store,
    session_uuid,
    source_provider,
    source_provider_data,
    destination_provider,
    mtv_namespace,
    ocp_admin_client,
):
    storage_map_list: list[dict[str, Any]] = []
    for storage in source_provider_data["storages"]:
        storage_map_list.append({
            "destination": {"storageClass": py_config["storage_class"]},
            "source": storage,
        })

    storage_map = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=StorageMap,
        client=ocp_admin_client,
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-{py_config['storage_class']}-storage-map",
        namespace=mtv_namespace,
        mapping=storage_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    )
    yield storage_map


@pytest.fixture(scope="session")
def destination_ocp_secret(fixture_store, ocp_admin_client, session_uuid, mtv_namespace):
    api_key: str = ocp_admin_client.configuration.api_key.get("authorization")
    if not api_key:
        raise ValueError("API key not found in configuration, please login with `oc login` first")

    secret = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Secret,
        name=f"{session_uuid}-ocp-secret",
        namespace=mtv_namespace,
        # API key format: 'Bearer sha256~<token>', split it to get token.
        string_data={"token": api_key.split()[-1], "insecureSkipVerify": "true"},
    )
    yield secret


@pytest.fixture(scope="session")
def destination_ocp_provider(fixture_store, destination_ocp_secret, ocp_admin_client, session_uuid, mtv_namespace):
    provider_name: str = f"{session_uuid}-ocp-provider"
    provider = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Provider,
        name=provider_name,
        namespace=mtv_namespace,
        secret_name=destination_ocp_secret.name,
        secret_namespace=destination_ocp_secret.namespace,
        url=ocp_admin_client.configuration.host,
        provider_type=Provider.ProviderType.OPENSHIFT,
    )
    yield CNVProvider(ocp_resource=provider)


@pytest.fixture(scope="session")
def remote_network_migration_map(
    fixture_store,
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
    network_map = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=NetworkMap,
        name=f"{session_uuid}-networkmap",
        namespace=mtv_namespace,
        mapping=network_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_ocp_provider.ocp_resource.name,
        destination_provider_namespace=destination_ocp_provider.ocp_resource.namespace,
    )
    yield network_map


@pytest.fixture(scope="session")
def remote_storage_migration_map(
    fixture_store,
    source_provider,
    source_provider_data,
    destination_ocp_provider,
    session_uuid,
    mtv_namespace,
    ocp_admin_client,
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

    storage_map = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=StorageMap,
        name=f"{session_uuid}-storagemap",
        namespace=mtv_namespace,
        mapping=storage_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_ocp_provider.ocp_resource.name,
        destination_provider_namespace=destination_ocp_provider.ocp_resource.namespace,
    )
    yield storage_map


@pytest.fixture(scope="session")
def source_provider_host_secret(
    fixture_store, session_uuid, source_provider, source_provider_data, mtv_namespace, ocp_admin_client
):
    if source_provider_data.get("host_list"):
        host = source_provider_data["host_list"][0]
        name = generate_name_with_uuid(
            name=f"{session_uuid}-{source_provider_data['fqdn']}-{host['migration_host_ip']}-{host['migration_host_id']}"
        )
        string_data: dict[str, str] = {
            "user": host["user"],
            "password": host["password"],
        }
        secret = create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=Secret,
            client=ocp_admin_client,
            name=name,
            namespace=mtv_namespace,
            string_data=string_data,
        )
        yield secret
    else:
        yield


@pytest.fixture(scope="session")
def source_provider_host(
    fixture_store,
    session_uuid,
    source_provider,
    source_provider_data,
    mtv_namespace,
    source_provider_host_secret,
    ocp_admin_client,
):
    if source_provider_data.get("host_list"):
        _host = source_provider_data["host_list"][0]
        create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=Host,
            client=ocp_admin_client,
            name=f"{source_provider_data['fqdn']}-{_host['migration_host_ip']}-{_host['migration_host_id']}",
            namespace=mtv_namespace,
            ip_address=_host["migration_host_ip"],
            host_id=_host["migration_host_id"],
            provider_name=source_provider.ocp_resource.name,
            provider_namespace=source_provider.ocp_resource.namespace,
            secret_name=source_provider_host_secret.name,
            secret_namespace=source_provider_host_secret.namespace,
        )
        yield _host

    else:
        yield


@pytest.fixture(scope="session")
def prehook(fixture_store, session_uuid, ocp_admin_client, mtv_namespace):
    pre_hook_dict: dict[str, str] = py_config["hook_dict"]["prehook"]
    hook = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Hook,
        client=ocp_admin_client,
        name=pre_hook_dict["name"],
        namespace=mtv_namespace,
        playbook=pre_hook_dict["payload"],
    )
    yield hook


@pytest.fixture(scope="session")
def posthook(fixture_store, session_uuid, ocp_admin_client, mtv_namespace):
    posthook_dict: dict[str, str] = py_config["hook_dict"]["posthook"]
    hook = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Hook,
        client=ocp_admin_client,
        name=posthook_dict["name"],
        namespace=mtv_namespace,
        playbook=posthook_dict["payload"],
    )
    yield hook


@pytest.fixture(scope="function")
def plans(fixture_store, target_namespace, ocp_admin_client, source_provider, request):
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
        and all([vm.get("source_vm_power") == "on" for vm in virtual_machines])
        and plan.get("pre_copies_before_cut_over")
    ):
        LOGGER.info("Starting Data Upload to source VMs")
        start_source_vm_data_upload_vmware(vmware_provider=source_provider, vm_names_list=vm_names_list)

    yield request.param

    for vm in virtual_machines:
        vm_obj = VirtualMachine(
            client=ocp_admin_client,
            name=vm["name"],
            namespace=target_namespace,
        )
        fixture_store["teardown"].setdefault(vm_obj.kind, []).append({
            "name": vm_obj.name,
            "namespace": vm_obj.namespace,
            "module": vm_obj.__module__,
        })

    for pod in Pod.get(client=ocp_admin_client, namespace=target_namespace):
        if plan["name"] in pod.name:
            fixture_store["teardown"].setdefault(pod.kind, []).append({
                "name": pod.name,
                "namespace": pod.namespace,
                "module": pod.__module__,
            })


@pytest.fixture(scope="session")
def forklift_pods_state(ocp_admin_client: DynamicClient) -> None:
    def _get_not_running_pods() -> bool:
        not_running_pods: list[str] = []

        for pod in Pod.get(dyn_client=ocp_admin_client, namespace=py_config["mtv_namespace"]):
            if pod.name.startswith("forklift-"):
                if pod.status not in (pod.Status.RUNNING, pod.Status.SUCCEEDED):
                    not_running_pods.append(pod.name)

        if not_running_pods:
            raise ForkliftPodsNotRunningError(f"Some of the forklift pods are not running: {not_running_pods}")

        return True

    for sample in TimeoutSampler(
        func=_get_not_running_pods,
        sleep=1,
        wait_timeout=60 * 5,
        exceptions_dict={ForkliftPodsNotRunningError: [], NotFoundError: []},
    ):
        if sample:
            return
