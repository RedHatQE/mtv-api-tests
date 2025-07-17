from __future__ import annotations

import json
import logging
import multiprocessing
import os
import pickle
import shutil
from pathlib import Path
from shutil import rmtree
from typing import Any, Generator

import pytest
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor, get_client
from ocp_resources.secret import Secret
from ocp_resources.storage_class import StorageClass
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.virtual_machine import VirtualMachine
from pytest_harvest import get_fixture_store
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutSampler

from exceptions.exceptions import ForkliftPodsNotRunningError, RemoteClusterAndLocalCluterNamesError
from libs.base_provider import BaseProvider
from libs.forklift_inventory import (
    ForkliftInventory,
    OpenshiftForkliftInventory,
    OpenstackForliftinventory,
    OvaForkliftInventory,
    OvirtForkliftInventory,
    VsphereForkliftInventory,
)
from libs.providers.openshift import OCPProvider
from utilities.logger import separator, setup_logging
from utilities.must_gather import run_must_gather
from utilities.prometheus import prometheus_monitor_deamon
from utilities.pytest_utils import SessionTeardownError, collect_created_resources, prepare_base_path, session_teardown
from utilities.resources import create_and_store_resource
from utilities.utils import (
    create_source_cnv_vms,
    create_source_provider,
    generate_name_with_uuid,
    get_value_from_py_config,
)

RESULTS_PATH = Path("./.xdist_results/")
RESULTS_PATH.mkdir(exist_ok=True)
LOGGER = logging.getLogger(__name__)
BASIC_LOGGER = logging.getLogger("basic")


# Pytest start


def pytest_addoption(parser):
    data_collector_group = parser.getgroup(name="DataCollector")
    teardown_group = parser.getgroup(name="Teardown")
    openshift_python_wrapper_group = parser.getgroup(name="Openshift Python Wrapper")
    data_collector_group.addoption("--skip-data-collector", action="store_true", help="Collect data for failed tests")
    data_collector_group.addoption(
        "--data-collector-path", help="Path to store collected data for failed tests", default=".data-collector"
    )
    teardown_group.addoption(
        "--skip-teardown", action="store_true", help="Do not teardown resource created by the tests"
    )
    openshift_python_wrapper_group.addoption(
        "--openshift-python-wrapper-log-debug", action="store_true", help="Enable debug logging in the wrapper"
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
    required_config = ("storage_class", "source_provider_type", "source_provider_version", "target_ocp_version")

    if not (session.config.getoption("--setupplan") or session.config.getoption("--collectonly")):
        for _req in required_config:
            if not py_config.get(_req):
                pytest.exit(reason=f"Some required config is missing {required_config}", returncode=1)

    _session_store = get_fixture_store(session)
    _session_store["teardown"] = {}

    if not session.config.getoption("skip_data_collector"):
        _data_collector_path = Path(session.config.getoption("data_collector_path"))
        prepare_base_path(base_path=_data_collector_path)

    tests_log_file = session.config.getoption("log_file") or "pytest-tests.log"
    if os.path.exists(tests_log_file):
        Path(tests_log_file).unlink(missing_ok=True)

    _log_level: int | str = session.config.getoption("log_cli_level") or logging.INFO

    if isinstance(_log_level, str):
        _log_level = logging.getLevelNamesMapping()[_log_level]

    if session.config.getoption("openshift_python_wrapper_log_debug"):
        os.environ["OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL"] = "DEBUG"

    session.config.option.log_listener = setup_logging(
        log_file=tests_log_file,
        log_level=_log_level,
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

    _data_collector_path = Path(session.config.getoption("data_collector_path"))

    if not session.config.getoption("skip_data_collector"):
        collect_created_resources(session_store=_session_store, data_collector_path=_data_collector_path)

    if session.config.getoption("skip_teardown"):
        LOGGER.warning("User requested to skip teardown of resources")

    else:
        # TODO: Maybe we need to check session_teardown return and fail the run if any leftovers
        try:
            session_teardown(session_store=_session_store)
        except SessionTeardownError as exp:
            LOGGER.error(f"the following resources was left after tests are finished: {exp}")
            if not session.config.getoption("skip_data_collector"):
                run_must_gather(data_collector_path=_data_collector_path)

    shutil.rmtree(path=session.config.option.basetemp, ignore_errors=True)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    reporter.summary_stats()


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        item.name = f"{item.name}-{py_config.get('source_provider_type')}-{py_config.get('source_provider_version')}-{py_config.get('storage_class')}"


def pytest_exception_interact(node, call, report):
    if not node.session.config.getoption("skip_data_collector"):
        _session_store = get_fixture_store(node.session)
        _data_collector_path = Path(f"{node.session.config.getoption('data_collector_path')}/{node.name}")
        test_name = node._pyfuncitem.name
        plans = _session_store["teardown"].get("Plan", [])
        plan = [plan for plan in plans if plan["test_name"] == test_name]
        plan = plan[0] if plan else None

        run_must_gather(data_collector_path=_data_collector_path, plan=plan)


# https://smarie.github.io/python-pytest-harvest/#pytest-x-dist
def pytest_harvest_xdist_init():
    # reset the recipient folder
    if RESULTS_PATH.exists():
        rmtree(RESULTS_PATH)

    RESULTS_PATH.mkdir(exist_ok=False)
    return True


def pytest_harvest_xdist_worker_dump(worker_id, session_items, fixture_store):
    # persist session_items and fixture_store in the file system
    with open(RESULTS_PATH / (f"{worker_id}.pkl"), "wb") as f:
        try:
            pickle.dump((session_items, fixture_store), f)
        except Exception as exp:
            LOGGER.warning(f"Error while pickling worker {worker_id}'s harvested results: [{exp.__class__}] {exp}")

    return True


def pytest_harvest_xdist_load():
    # restore the saved objects from file system
    workers_saved_material = {}

    for pkl_file in RESULTS_PATH.glob("*.pkl"):
        wid = pkl_file.stem

        with pkl_file.open("rb") as f:
            workers_saved_material[wid] = pickle.load(f)

    return workers_saved_material


def pytest_harvest_xdist_cleanup():
    # delete all temporary pickle files
    rmtree(RESULTS_PATH)
    return True


# Pytest end


@pytest.fixture(scope="session", autouse=True)
def autouse_fixtures(source_provider_data, prometheus_monitor, nfs_storage_profile, forklift_pods_state):
    # source_provider_data called here to fail fast in provider not found in the providers list from config
    yield


@pytest.fixture(scope="session")
def prometheus_monitor(ocp_admin_client: DynamicClient) -> Generator[None, Any, Any]:
    try:
        proc = multiprocessing.Process(
            target=prometheus_monitor_deamon,
            kwargs={"ocp_admin_client": ocp_admin_client},
        )

        proc.start()
        yield
        proc.kill()

    except Exception:
        yield


@pytest.fixture(scope="session")
def target_namespace(fixture_store, session_uuid, ocp_admin_client):
    """create the target namespace for MTV migrations"""
    label: dict[str, str] = {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
        "mutatevirtualmachines.kubemacpool.io": "ignore",
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
        storage_profile = StorageProfile(client=ocp_admin_client, name=nfs, ensure_exists=True)

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
        client=ocp_admin_client,
        name="forklift-controller",
        namespace=mtv_namespace,
        ensure_exists=True,
    )

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
        name=py_config.get("destination_provider_name", "host"),
        namespace=mtv_namespace,
        client=ocp_admin_client,
        ensure_exists=True,
    )

    return OCPProvider(ocp_resource=provider)


@pytest.fixture(scope="session")
def source_provider_data():
    _source_provider_key = f"{py_config['source_provider_type']}-{py_config['source_provider_version']}"
    _source_provider = py_config["source_providers_dict"][_source_provider_key]

    if not _source_provider:
        raise ValueError(f"Source provider {_source_provider['type']}-{_source_provider['version']} not found")

    return _source_provider


@pytest.fixture(scope="session")
def source_provider(
    fixture_store,
    session_uuid,
    source_provider_data,
    target_namespace,
    ocp_admin_client,
    tmp_path_factory,
    destination_ocp_secret,
):
    with create_source_provider(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        config=py_config,
        source_provider_data=source_provider_data,
        namespace=target_namespace,
        admin_client=ocp_admin_client,
        tmp_dir=tmp_path_factory,
        target_namespace=target_namespace,
        ocp_admin_client=ocp_admin_client,
        destination_ocp_secret=destination_ocp_secret,
        insecure=get_value_from_py_config(value="insecure_verify_skip"),
    ) as _source_provider:
        yield _source_provider

    _source_provider.disconnect()


@pytest.fixture(scope="session")
def multus_network_name(fixture_store, session_uuid, target_namespace, ocp_admin_client, multus_cni_config):
    bridge_type_and_name = "cnv-bridge"

    create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=NetworkAttachmentDefinition,
        client=ocp_admin_client,
        name=bridge_type_and_name,
        cni_type=bridge_type_and_name,
        namespace=target_namespace,
        config=multus_cni_config,
    )

    yield bridge_type_and_name


@pytest.fixture(scope="session")
def destination_ocp_secret(fixture_store, ocp_admin_client, session_uuid, target_namespace):
    api_key: str = ocp_admin_client.configuration.api_key.get("authorization")
    if not api_key:
        raise ValueError("API key not found in configuration, please login with `oc login` first")

    secret = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Secret,
        name=f"{session_uuid}-ocp-secret",
        namespace=target_namespace,
        # API key format: 'Bearer sha256~<token>', split it to get token.
        string_data={"token": api_key.split()[-1], "insecureSkipVerify": "true"},
    )
    yield secret


@pytest.fixture(scope="session")
def destination_ocp_provider(fixture_store, destination_ocp_secret, ocp_admin_client, session_uuid, target_namespace):
    provider_name: str = f"{session_uuid}-ocp-provider"
    provider = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Provider,
        name=provider_name,
        namespace=target_namespace,
        secret_name=destination_ocp_secret.name,
        secret_namespace=destination_ocp_secret.namespace,
        url=ocp_admin_client.configuration.host,
        provider_type=Provider.ProviderType.OPENSHIFT,
    )
    yield OCPProvider(ocp_resource=provider)


@pytest.fixture(scope="function")
def plan(
    fixture_store,
    session_uuid,
    target_namespace,
    ocp_admin_client,
    source_provider,
    request,
    multus_network_name,
    source_vms_namespace,
    source_vms_network,
):
    plan: dict[str, Any] = request.param
    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]

    if source_provider.type != Provider.ProviderType.OVA:
        openshift_source_provider: bool = source_provider.type == Provider.ProviderType.OPENSHIFT

        if openshift_source_provider:
            create_source_cnv_vms(
                fixture_store=fixture_store,
                dyn_client=ocp_admin_client,
                vms=virtual_machines,
                namespace=source_vms_namespace,
                network_name=multus_network_name,
                session_uuid=session_uuid,
            )
        for vm in virtual_machines:
            source_vm_details = source_provider.vm_dict(name=vm["name"], namespace=source_vms_namespace, source=True)
            provider_vm_api = source_vm_details["provider_vm_api"]

            vm["snapshots_before_migration"] = source_vm_details["snapshots_data"]

            if vm.get("source_vm_power") == "on":
                source_provider.start_vm(provider_vm_api)

            elif vm.get("source_vm_power") == "off":
                source_provider.stop_vm(provider_vm_api)

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
        fixture_store["teardown"].setdefault(pod.kind, []).append({
            "name": pod.name,
            "namespace": pod.namespace,
            "module": pod.__module__,
        })


@pytest.fixture(scope="session")
def forklift_pods_state(ocp_admin_client: DynamicClient) -> None:
    def _get_not_running_pods(_admin_client: DynamicClient) -> bool:
        controller_pod: str | None = None
        not_running_pods: list[str] = []

        for pod in Pod.get(dyn_client=_admin_client, namespace=py_config["mtv_namespace"]):
            if pod.name.startswith("forklift-"):
                if pod.name.startswith("forklift-controller"):
                    controller_pod = pod

                if pod.status not in (pod.Status.RUNNING, pod.Status.SUCCEEDED):
                    not_running_pods.append(pod.name)

        if not controller_pod:
            raise ForkliftPodsNotRunningError("Forklift controller pod not found")

        if not_running_pods:
            raise ForkliftPodsNotRunningError(f"Some of the forklift pods are not running: {not_running_pods}")

        return True

    for sample in TimeoutSampler(
        func=_get_not_running_pods,
        _admin_client=ocp_admin_client,
        sleep=1,
        wait_timeout=60 * 5,
        exceptions_dict={ForkliftPodsNotRunningError: [], NotFoundError: []},
    ):
        if sample:
            return


@pytest.fixture(scope="session")
def source_provider_inventory(
    ocp_admin_client: DynamicClient, mtv_namespace: str, source_provider: BaseProvider
) -> ForkliftInventory:
    providers = {
        Provider.ProviderType.OVA: OvaForkliftInventory,
        Provider.ProviderType.RHV: OvirtForkliftInventory,
        Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
        Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
        Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
    }
    provider_instance = providers.get(source_provider.type)

    if not provider_instance:
        raise ValueError(f"Provider {source_provider.type} not implemented")

    return provider_instance(  # type: ignore
        client=ocp_admin_client, namespace=mtv_namespace, provider_name=source_provider.ocp_resource.name
    )


@pytest.fixture(scope="session")
def source_vms_namespace(source_provider, fixture_store, ocp_admin_client, session_uuid):
    if source_provider.type == Provider.ProviderType.OPENSHIFT:
        namespace = create_and_store_resource(
            resource=Namespace,
            fixture_store=fixture_store,
            session_uuid=fixture_store["session_uuid"],
            client=ocp_admin_client,
            name=f"{session_uuid}-source-vms",
            label={"mutatevirtualmachines.kubemacpool.io": "ignore"},
        )
        return namespace.name


@pytest.fixture(scope="session")
def source_vms_network(
    source_provider, source_vms_namespace, ocp_admin_client, fixture_store, session_uuid, multus_cni_config
):
    if source_provider.type == Provider.ProviderType.OPENSHIFT:
        ceph_virtualization_sc = StorageClass(
            client=ocp_admin_client, name="ocs-storagecluster-ceph-rbd-virtualization", ensure_exists=True
        )
        ResourceEditor(
            patches={
                ceph_virtualization_sc: {
                    "metadata": {
                        "annotations": {StorageClass.Annotations.IS_DEFAULT_VIRT_CLASS: "true"},
                        "name": ceph_virtualization_sc.name,
                    }
                }
            }
        ).update()

        bridge_type_and_name = "cnv-bridge"

        create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=NetworkAttachmentDefinition,
            client=ocp_admin_client,
            name=bridge_type_and_name,
            cni_type=bridge_type_and_name,
            namespace=source_vms_namespace,
            config=multus_cni_config,
        )

        return bridge_type_and_name


@pytest.fixture(scope="session")
def multus_cni_config() -> str:
    bridge_type_and_name = "cnv-bridge"
    config = {"cniVersion": "0.3.1", "type": f"{bridge_type_and_name}", "bridge": f"{bridge_type_and_name}"}
    return json.dumps(config)
