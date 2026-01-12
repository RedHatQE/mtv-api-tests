from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import random
import shutil
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from shutil import rmtree
from typing import Any
from urllib.error import URLError

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from ocp_resources.route import Route
from ocp_resources.secret import Secret
from ocp_resources.storage_class import StorageClass
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.virtual_machine import VirtualMachine
from pytest_harvest import get_fixture_store
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutSampler

from exceptions.exceptions import (
    ForkliftPodsNotRunningError,
    MissingProvidersFileError,
    RemoteClusterAndLocalCluterNamesError,
)
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
from utilities.copyoffload_migration import get_copyoffload_credential
from utilities.esxi import install_ssh_key_on_esxi, remove_ssh_key_from_esxi
from utilities.logger import separator, setup_logging
from utilities.mtv_migration import get_vm_suffix
from utilities.must_gather import run_must_gather
from utilities.naming import generate_name_with_uuid
from utilities.pytest_utils import (
    collect_created_resources,
    prepare_base_path,
    session_teardown,
)
from utilities.resources import create_and_store_resource
from utilities.utils import (
    create_source_cnv_vms,
    create_source_provider,
    get_cluster_client,
    get_value_from_py_config,
    is_mtv_version_supported,
)
from utilities.virtctl import download_virtctl_from_cluster
from utilities.ssh_utils import SSHConnectionManager

RESULTS_PATH = Path("./.xdist_results/")
RESULTS_PATH.mkdir(exist_ok=True)
LOGGER = logging.getLogger(__name__)
BASIC_LOGGER = logging.getLogger("basic")
PROMETHEUS_QUERY_TIMEOUT_SECONDS: int = 30


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
        "--openshift-python-wrapper-log-debug",
        action="store_true",
        help="Enable debug logging in the openshift-python-wrapper module",
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
    required_config = ("storage_class", "source_provider_type", "source_provider_version")

    if not (session.config.getoption("--setupplan") or session.config.getoption("--collectonly")):
        missing_configs: list[str] = []

        for _req in required_config:
            if not py_config.get(_req):
                missing_configs.append(_req)

        if missing_configs:
            pytest.exit(reason=f"Some required config is missing {required_config=} - {missing_configs=}", returncode=1)

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
        except Exception as exp:
            LOGGER.error(f"the following resources was left after tests are finished: {exp}")
            if not session.config.getoption("skip_data_collector"):
                run_must_gather(data_collector_path=_data_collector_path)

    shutil.rmtree(path=session.config.option.basetemp, ignore_errors=True)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    reporter.summary_stats()


def pytest_collection_modifyitems(session, config, items):
    _session_store = get_fixture_store(session)
    vms_for_current_session: set = set()

    for item in items:
        item.name = f"{item.name}-{py_config.get('source_provider_type')}-{py_config.get('source_provider_version')}-{py_config.get('storage_class')}"
        for _vm in py_config["tests_params"][item.originalname]["virtual_machines"]:
            vms_for_current_session.add(_vm["name"])

    _session_store["vms_for_current_session"] = vms_for_current_session

    if not (session.config.getoption("--setupplan") or session.config.getoption("--collectonly")):
        LOGGER.info(f"Base VMS names for current session:\n {'\n'.join(vms_for_current_session)}")


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
def autouse_fixtures(
    source_provider_data, nfs_storage_profile, base_resource_name, forklift_pods_state, virtctl_binary
):
    # source_provider_data called here to fail fast in provider not found in the providers list from config
    yield


@pytest.fixture(scope="session")
def base_resource_name(fixture_store, session_uuid, source_provider_data):
    _name = f"{session_uuid}-source-{source_provider_data['type']}-{source_provider_data['version'].replace('.', '-')}"

    # Add copyoffload indicator for Plan/StorageMap/NetworkMap names
    if "copyoffload" in source_provider_data:
        _name = f"{_name}-xcopy"

    fixture_store["base_resource_name"] = _name


@pytest.fixture(scope="session")
def source_providers() -> dict[str, dict[str, Any]]:
    _provider_file_name = ".providers.json"
    providers_file = Path(_provider_file_name)
    if not providers_file.exists():
        raise MissingProvidersFileError(f"{_provider_file_name} file is missing")

    with open(providers_file, "r") as fd:
        source_providers_dict = json.load(fd)

    return source_providers_dict


@pytest.fixture(scope="session")
def target_namespace(fixture_store, session_uuid, ocp_admin_client):
    """create the target namespace for MTV migrations"""
    label: dict[str, str] = {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
        "mutatevirtualmachines.kubemacpool.io": "ignore",
    }

    _target_namespace: str = py_config["target_namespace_prefix"]

    # Remove prefix value to avoid duplication - session_uuid is already generated from this prefix
    _target_namespace = _target_namespace.replace("auto", "")

    # Generate a unique namespace name to avoid conflicts and support run multiple runs with the same provider configs
    unique_namespace_name = f"{session_uuid}{_target_namespace}"[:63]
    fixture_store["target_namespace"] = unique_namespace_name

    namespace = create_and_store_resource(
        fixture_store=fixture_store,
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
    _session_uuid = generate_name_with_uuid(name="auto")
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
    _client = get_cluster_client()

    if remote_cluster_name := get_value_from_py_config("remote_ocp_cluster"):
        if remote_cluster_name not in _client.configuration.host:
            raise RemoteClusterAndLocalCluterNamesError("Remote cluster must be the same as local cluster.")

    yield _client


@pytest.fixture(scope="session")
def virtctl_binary(ocp_admin_client):
    """
    Download and configure virtctl binary from the cluster.

    This fixture ensures virtctl is available in PATH for all tests
    that need to interact with VMs via virtctl commands.
    """
    return download_virtctl_from_cluster(client=ocp_admin_client)


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
                    "controller_precopy_interval": str(snapshots_interval),
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
def destination_provider(session_uuid, ocp_admin_client, target_namespace, fixture_store):
    kind_dict = {
        "apiVersion": "forklift.konveyor.io/v1beta1",
        "kind": "Provider",
        "metadata": {"name": f"{session_uuid}-local-ocp-provider", "namespace": target_namespace},
        "spec": {"secret": {}, "type": "openshift", "url": ""},
    }

    provider = create_and_store_resource(
        fixture_store=fixture_store,
        resource=Provider,
        kind_dict=kind_dict,
        client=ocp_admin_client,
    )

    return OCPProvider(ocp_resource=provider, fixture_store=fixture_store)


@pytest.fixture(scope="session")
def source_provider_data(source_providers, fixture_store):
    _source_provider_key = f"{py_config['source_provider_type']}-{py_config['source_provider_version']}"
    _source_provider = source_providers[_source_provider_key]

    if not _source_provider:
        raise ValueError(f"Source provider {_source_provider['type']}-{_source_provider['version']} not found")

    fixture_store["source_provider_data"] = _source_provider
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
        source_provider_data=source_provider_data,
        namespace=target_namespace,
        admin_client=ocp_admin_client,
        tmp_dir=tmp_path_factory,
        ocp_admin_client=ocp_admin_client,
        destination_ocp_secret=destination_ocp_secret,
        insecure=get_value_from_py_config(value="source_provider_insecure_skip_verify"),
    ) as _source_provider:
        yield _source_provider

    _source_provider.disconnect()


@pytest.fixture(scope="function")
def multus_network_name(
    fixture_store, target_namespace, ocp_admin_client, multus_cni_config, source_provider_inventory, request
):
    """
    Create NADs based on network requirements with unique names per test.
    Automatically detects number of networks and creates NADs with test-unique naming:
    - cb-{4-char-hash}-1, cb-{4-char-hash}-2, etc. (e.g., "cb-a1b2-1" = 9 chars)

    The unique test hash prevents conflicts when running tests in parallel.
    Names are kept under 15 characters to comply with Linux bridge interface name limits.
    """
    # Validate test configuration structure
    if not isinstance(request.param, dict):
        raise TypeError(f"request.param must be dict, got {type(request.param)}")

    test_config: dict[str, Any] = request.param

    if "virtual_machines" not in test_config:
        raise KeyError("Test configuration missing required 'virtual_machines' key")

    if not isinstance(test_config["virtual_machines"], list):
        raise TypeError("test_config['virtual_machines'] must be list")

    # Generate unique identifier based on test name to avoid conflicts in parallel execution
    test_name = request.node.name
    short_hash = hashlib.md5(test_name.encode()).hexdigest()[:4]
    base_name = f"cb-{short_hash}"

    LOGGER.info(f"Creating NADs with unique base name: {base_name} (test: {test_name})")

    created_nads = []

    # Get VM names from the test configuration (same way as plan fixture)
    vms = [vm["name"] for vm in test_config["virtual_machines"]]
    LOGGER.info(f"Found VMs from test config: {vms}")

    # Get network count directly from source provider inventory
    networks = source_provider_inventory.vms_networks_mappings(vms=vms)

    if not networks:
        raise ValueError(f"No networks found for VMs {vms}. VMs must have at least one network interface.")

    # Calculate how many multus NADs we need (all networks except the first one)
    multus_count = max(0, len(networks) - 1)  # First network goes to pod, rest to multus

    # Create all required NADs with consistent naming
    for i in range(1, multus_count + 1):
        nad_name = f"{base_name}-{i}"

        # Use the provided config for the first NAD, custom config for others
        if i == 1:
            config = multus_cni_config
        else:
            cni_config = {"cniVersion": "0.3.1", "type": "bridge", "bridge": nad_name}
            config = json.dumps(cni_config)

        create_and_store_resource(
            fixture_store=fixture_store,
            resource=NetworkAttachmentDefinition,
            client=ocp_admin_client,
            namespace=target_namespace,
            config=config,
            name=nad_name,
        )

        created_nads.append(nad_name)
        LOGGER.info(f"Created NAD: {nad_name} in namespace {target_namespace}")

    LOGGER.info(f"Created {len(created_nads)} NADs for migration: {created_nads}")

    # Return the base name - consuming code will generate the same indexed names
    # This maintains the contract: fixture creates NADs, returns base name for generation
    yield base_name


@pytest.fixture(scope="function")
def vm_ssh_connections(fixture_store, destination_provider, target_namespace, ocp_admin_client):
    """
    Fixture to manage SSH connections to migrated VMs using python-rrmngmnt.

    Usage:
        def test_vm_ssh_access(vm_ssh_connections):
            ssh_conn = vm_ssh_connections.create(vm_name="my-vm", username="root", password="pass")
            with ssh_conn:
                from pyhelper_utils.shell import run_ssh_commands
                results = run_ssh_commands(ssh_conn.rrmngmnt_host, ["whoami"])
                host = ssh_conn.get_rrmngmnt_host()  # Access rrmngmnt's rich API
                host.fs.put("/local/file", "/remote/file")
                host.package_management.install("htop")
    """
    manager = SSHConnectionManager(
        provider=destination_provider,
        namespace=target_namespace,
        fixture_store=fixture_store,
        ocp_client=ocp_admin_client,
    )

    yield manager

    # Cleanup
    manager.cleanup_all()


@pytest.fixture(scope="session")
def destination_ocp_secret(fixture_store, ocp_admin_client, target_namespace):
    api_key: str = ocp_admin_client.configuration.api_key.get("authorization")
    if not api_key:
        raise ValueError("API key not found in configuration")

    secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        # API key format: 'Bearer sha256~<token>', split it to get token.
        string_data={"token": api_key.split()[-1], "insecureSkipVerify": "true"},
    )
    yield secret


@pytest.fixture(scope="session")
def destination_ocp_provider(fixture_store, destination_ocp_secret, ocp_admin_client, session_uuid, target_namespace):
    provider = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Provider,
        name=f"{session_uuid}-destination-ocp-provider",
        namespace=target_namespace,
        secret_name=destination_ocp_secret.name,
        secret_namespace=destination_ocp_secret.namespace,
        url=ocp_admin_client.configuration.host,
        provider_type=Provider.ProviderType.OPENSHIFT,
    )
    yield OCPProvider(ocp_resource=provider, fixture_store=fixture_store)


def _get_worker_nodes(v1: client.CoreV1Api) -> list[str]:
    """Get list of worker node names."""
    return [
        node.metadata.name for node in v1.list_node().items if "node-role.kubernetes.io/worker" in node.metadata.labels
    ]


def _get_prometheus_url(ocp_admin_client: DynamicClient) -> str | None:
    """Get Prometheus/Thanos querier URL from OpenShift monitoring."""
    try:
        thanos_route = Route(
            client=ocp_admin_client,
            name="thanos-querier",
            namespace="openshift-monitoring",
        )
        if thanos_route.exists:
            return f"https://{thanos_route.instance.spec.host}"
    except (NotFoundError, ApiException) as e:
        LOGGER.warning(f"Could not get Thanos querier route: {e}")
    return None


def _query_prometheus(url: str, query: str, token: str, *, insecure: bool = True) -> list[dict[str, Any]] | None:
    """Query Prometheus API and return results."""
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme != "https":
        LOGGER.warning(f"Invalid URL scheme '{parsed_url.scheme}' for Prometheus query, expected 'https'")
        return None

    try:
        request_url = f"{url}/api/v1/query?query={urllib.parse.quote(query)}"
        req = urllib.request.Request(request_url)
        req.add_header("Authorization", f"Bearer {token}")

        ssl_context = ssl.create_default_context()
        if insecure:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, context=ssl_context, timeout=PROMETHEUS_QUERY_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode())
            if data.get("status") == "success":
                return data.get("data", {}).get("result", [])
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        LOGGER.warning(f"Prometheus query failed for '{query}': {e}")
    return None


def _parse_prometheus_memory_metrics(
    worker_nodes: list[str], prom_url: str, token: str
) -> dict[str, dict[str, int]] | None:
    """Query Prometheus for memory metrics and return structured data."""
    allocatable_query = 'kube_node_status_allocatable{resource="memory"}'
    requested_query = 'sum by (node) (kube_pod_container_resource_requests{resource="memory"} * on(namespace, pod) group_left() (kube_pod_status_phase{phase="Running"} == 1))'

    allocatable_result = _query_prometheus(prom_url, allocatable_query, token)
    requested_result = _query_prometheus(prom_url, requested_query, token)

    if not allocatable_result:
        return None

    metrics: dict[str, dict[str, int]] = {}
    for item in allocatable_result:
        node = item.get("metric", {}).get("node")
        if node in worker_nodes:
            raw_value = item.get("value")
            if isinstance(raw_value, (list, tuple)) and len(raw_value) >= 2 and raw_value[1]:
                try:
                    value = int(float(raw_value[1]))
                except (ValueError, TypeError):
                    value = 0
            else:
                value = 0
            metrics.setdefault(node, {})["allocatable"] = value

    if requested_result:
        for item in requested_result:
            node = item.get("metric", {}).get("node")
            if node in worker_nodes and node in metrics:
                raw_value = item.get("value")
                if isinstance(raw_value, (list, tuple)) and len(raw_value) >= 2 and raw_value[1]:
                    try:
                        value = int(float(raw_value[1]))
                    except (ValueError, TypeError):
                        value = 0
                else:
                    value = 0
                metrics[node]["requested"] = value

    for node in metrics:
        metrics[node].setdefault("requested", 0)
        metrics[node]["available"] = metrics[node]["allocatable"] - metrics[node]["requested"]

    return metrics if metrics else None


def _select_node_by_available_memory(
    ocp_admin_client: DynamicClient,
    worker_nodes: list[str],
) -> str:
    """Select worker node with highest available memory using Prometheus metrics."""
    if not worker_nodes:
        raise ValueError("No worker nodes available for selection")

    auth_header = ocp_admin_client.configuration.api_key.get("authorization", "")
    token_parts = auth_header.split()
    token = token_parts[-1] if token_parts else ""
    if not token:
        LOGGER.warning("No auth token available, selecting random worker node")
        return random.choice(worker_nodes)

    prom_url = _get_prometheus_url(ocp_admin_client)
    if not prom_url:
        LOGGER.warning("Prometheus URL not found, selecting random worker node")
        return random.choice(worker_nodes)

    metrics = _parse_prometheus_memory_metrics(worker_nodes, prom_url, token)
    if not metrics:
        LOGGER.warning("No valid memory metrics available, selecting random worker node")
        return random.choice(worker_nodes)

    max_available = max(node_metrics["available"] for node_metrics in metrics.values())
    nodes_with_max = [node for node, node_metrics in metrics.items() if node_metrics["available"] == max_available]
    selected_node = random.choice(nodes_with_max)

    LOGGER.info(f"Selected node {selected_node} with highest available memory for scheduling")

    return selected_node


def _label_node(v1: client.CoreV1Api, node_name: str, label_key: str, label_value: str) -> None:
    """Apply label to a node."""
    node = v1.read_node(name=node_name)
    if not node.metadata.labels:
        node.metadata.labels = {}
    node.metadata.labels[label_key] = label_value
    v1.patch_node(name=node_name, body=node)


def _cleanup_node_label(v1: client.CoreV1Api, node_name: str, label_key: str) -> None:
    """Remove label from node using strategic merge patch."""
    try:
        patch_body = {"metadata": {"labels": {label_key: None}}}
        v1.patch_node(name=node_name, body=patch_body)
        LOGGER.info(f"Removed label {label_key} from node {node_name}")
    except ApiException as e:
        LOGGER.warning(f"Failed to cleanup label {label_key} from node {node_name}: {e}")


@pytest.fixture(scope="function")
def labeled_worker_node(session_uuid, ocp_admin_client, plan):
    """Label a worker node with test label for targetNodeSelector testing."""
    target_node_selector = plan.get("target_node_selector", {})
    if not target_node_selector:
        yield None
        return

    if not is_mtv_version_supported(ocp_admin_client, "2.10.0"):
        pytest.skip("targetNodeSelector requires MTV 2.10.0+")

    v1 = client.CoreV1Api(api_client=ocp_admin_client.client)
    worker_nodes = _get_worker_nodes(v1)

    if not worker_nodes:
        pytest.skip("No worker nodes found")

    target_node = _select_node_by_available_memory(ocp_admin_client, worker_nodes)

    label_key, config_value = next(iter(target_node_selector.items()))
    label_value = session_uuid if config_value == "auto" else config_value

    _label_node(v1, target_node, label_key, label_value)
    LOGGER.info(f"Labeled worker node {target_node} with {label_key}={label_value}")

    result = {"node_name": target_node, "label_key": label_key, "label_value": label_value}
    yield result

    _cleanup_node_label(v1, target_node, label_key)


@pytest.fixture(scope="function")
def labeled_vm(session_uuid, ocp_admin_client, plan):
    """Generate VM labels for targetLabels testing."""

    # Only create fixture if target_labels is configured
    target_labels = plan.get("target_labels", {})
    if not target_labels:
        yield None
        return

    # Feature IS configured - check MTV version support
    if not is_mtv_version_supported(ocp_admin_client, "2.10.0"):
        pytest.skip("targetLabels requires MTV 2.10.0+")

    # Generate label values
    vm_labels = {}
    for label_key, config_value in target_labels.items():
        if config_value == "auto":
            label_value = session_uuid
        else:
            label_value = config_value  # Use specified value
        vm_labels[label_key] = label_value
        LOGGER.info(f"Generated VM label: {label_key}={label_value}")

    result = {"vm_labels": vm_labels}
    LOGGER.info(f"labeled_vm fixture returning: {result}")

    yield result


@pytest.fixture(scope="function")
def plan(
    fixture_store,
    target_namespace,
    ocp_admin_client,
    source_provider,
    source_provider_inventory,
    request,
    multus_network_name,
    source_vms_namespace,
    source_vms_network,
):
    plan: dict[str, Any] = request.param
    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
    warm_migration = plan.get("warm_migration", False)

    # Override VM names from provider config if specified
    if hasattr(source_provider, "copyoffload_config") and source_provider.copyoffload_config:
        default_vm_override = source_provider.copyoffload_config.get("default_vm_name")
        if default_vm_override:
            for vm in virtual_machines:
                if vm.get("clone", False):  # Only override for cloned VMs
                    LOGGER.info(f"Overriding VM name '{vm['name']}' with '{default_vm_override}' from provider config")
                    vm["name"] = default_vm_override

    if source_provider.type != Provider.ProviderType.OVA:
        openshift_source_provider: bool = source_provider.type == Provider.ProviderType.OPENSHIFT

        vm_name_suffix = get_vm_suffix(warm_migration=warm_migration)

        if openshift_source_provider:
            create_source_cnv_vms(
                fixture_store=fixture_store,
                client=ocp_admin_client,
                vms=virtual_machines,
                namespace=source_vms_namespace,
                network_name=multus_network_name,
                vm_name_suffix=vm_name_suffix,
            )
        for vm in virtual_machines:
            # Get VM object first (without full vm_dict analysis)
            provider_vm_api = source_provider.get_vm_by_name(
                query=vm["name"],
                vm_name_suffix=vm_name_suffix,
                clone_vm=True,
                session_uuid=fixture_store["session_uuid"],
                clone_options=vm,
            )

            # Power state control: "on" = start VM, "off" = stop VM, not set = leave unchanged
            source_vm_power = vm.get("source_vm_power")  # Optional - if not set, VM power state unchanged
            if source_vm_power == "on":
                source_provider.start_vm(provider_vm_api)
                # Wait for guest info to become available (VMware only)
                if source_provider.type == Provider.ProviderType.VSPHERE:
                    source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=60)
            elif source_vm_power == "off":
                source_provider.stop_vm(provider_vm_api)

            # NOW call vm_dict() with VM in correct power state for guest info
            source_vm_details = source_provider.vm_dict(
                provider_vm_api=provider_vm_api,
                name=vm["name"],
                namespace=source_vms_namespace,
                clone=False,  # Already cloned above
                vm_name_suffix=vm_name_suffix,
                session_uuid=fixture_store["session_uuid"],
                clone_options=vm,
            )
            vm["name"] = source_vm_details["name"]

            # Wait for cloned VM to appear in Forklift inventory before proceeding
            # This is needed for external providers that Forklift needs to sync from
            # OVA is excluded because it doesn't clone VMs (uses pre-existing files)
            if source_provider.type != Provider.ProviderType.OVA:
                source_provider_inventory.wait_for_vm(name=vm["name"], timeout=300)

            provider_vm_api = source_vm_details["provider_vm_api"]

            vm["snapshots_before_migration"] = source_vm_details["snapshots_data"]
            # Store complete source VM data for post-migration checks
            vm["source_vm_data"] = source_vm_details

    yield plan

    for vm in plan["virtual_machines"]:
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

        for pod in Pod.get(client=_admin_client, namespace=py_config["mtv_namespace"]):
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
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

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
            client=ocp_admin_client,
            name=f"{session_uuid}-source-vms",
            label={"mutatevirtualmachines.kubemacpool.io": "ignore"},
        )
        return namespace.name


@pytest.fixture(scope="session")
def source_vms_network(source_provider, source_vms_namespace, ocp_admin_client, fixture_store, multus_cni_config):
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
            resource=NetworkAttachmentDefinition,
            client=ocp_admin_client,
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


@pytest.fixture(scope="function")
def copyoffload_config(source_provider, source_provider_data):
    """
    Validate copy-offload configuration before running copy-offload tests.

    This fixture performs all necessary validations:
    - Verifies vSphere provider type
    - Checks for copyoffload configuration
    - Validates storage credentials availability

    If any validation fails, the test will fail early with a clear error message.
    """
    # Validate that this is a vSphere provider
    if source_provider.type != Provider.ProviderType.VSPHERE:
        pytest.fail(
            f"Copy-offload tests require vSphere provider, but got '{source_provider.type}'. "
            "Check your provider configuration in .providers.json"
        )

    # Validate copy-offload configuration exists
    if "copyoffload" not in source_provider_data:
        pytest.fail(
            "Copy-offload configuration not found in source provider data. "
            "Add 'copyoffload' section to your provider in .providers.json"
        )

    config = source_provider_data["copyoffload"]

    # Validate required storage credentials are available (from either env vars or .providers.json)
    required_credentials = ["storage_hostname", "storage_username", "storage_password"]
    missing_credentials = []

    for cred in required_credentials:
        # Check if credential is available from either env var or config file
        if not get_copyoffload_credential(cred, config):
            missing_credentials.append(cred)

    if missing_credentials:
        pytest.fail(
            f"Required storage credentials not found: {missing_credentials}. "
            f"Add them to .providers.json copyoffload section or set environment variables: "
            f"{', '.join([f'COPYOFFLOAD_{c.upper()}' for c in missing_credentials])}"
        )

    LOGGER.info("✓ Copy-offload configuration validated successfully")


@pytest.fixture(scope="function")
def copyoffload_storage_secret(
    fixture_store,
    ocp_admin_client,
    target_namespace,
    source_provider_data,
    copyoffload_config,
):
    """
    Create a storage secret for copy-offload functionality.

    This fixture creates the storage secret required for copy-offload migrations
    with credentials from environment variables or .providers.json.

    Args:
        fixture_store: Pytest fixture store for resource tracking
        ocp_admin_client: OpenShift admin client
        target_namespace: Target namespace for the secret
        source_provider_data: Source provider configuration data
        copyoffload_config: Copy-offload configuration (validates prerequisites)

    Returns:
        Secret: Created storage secret resource
    """
    LOGGER.info("Creating copy-offload storage secret")

    copyoffload_cfg = source_provider_data["copyoffload"]

    # Get storage credentials from environment variables or provider config
    storage_hostname = get_copyoffload_credential("storage_hostname", copyoffload_cfg)
    storage_username = get_copyoffload_credential("storage_username", copyoffload_cfg)
    storage_password = get_copyoffload_credential("storage_password", copyoffload_cfg)

    if not all([storage_hostname, storage_username, storage_password]):
        raise ValueError(
            "Storage credentials are required. Set COPYOFFLOAD_STORAGE_HOSTNAME, COPYOFFLOAD_STORAGE_USERNAME, "
            "and COPYOFFLOAD_STORAGE_PASSWORD environment variables or include them in .providers.json"
        )

    # Validate storage vendor product
    storage_vendor = copyoffload_cfg.get("storage_vendor_product")
    if not storage_vendor:
        raise ValueError(
            "storage_vendor_product is required in copyoffload configuration. Valid values: 'ontap', 'vantara'"
        )

    # Base secret data
    secret_data = {
        "STORAGE_HOSTNAME": storage_hostname,
        "STORAGE_USERNAME": storage_username,
        "STORAGE_PASSWORD": storage_password,
    }

    # Add vendor-specific configuration
    if storage_vendor == "ontap":
        ontap_svm = get_copyoffload_credential("ontap_svm", copyoffload_cfg)
        if ontap_svm:
            secret_data["ONTAP_SVM"] = ontap_svm

    LOGGER.info(f"Creating storage secret for copy-offload with vendor: {storage_vendor}")

    storage_secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        string_data=secret_data,
    )

    LOGGER.info(f"✓ Copy-offload storage secret created: {storage_secret.name}")
    return storage_secret


@pytest.fixture(scope="function")
def setup_copyoffload_ssh(source_provider, source_provider_data, copyoffload_config):
    """
    Sets up SSH key on ESXi host for copy-offload if SSH method is enabled.

    Depends on copyoffload_config to ensure validation runs first.
    """
    copyoffload_cfg = source_provider_data["copyoffload"]  # Safe: copyoffload_config validates this exists
    if copyoffload_cfg.get("esxi_clone_method") != "ssh":
        LOGGER.info("SSH clone method not configured, skipping SSH key setup.")
        yield
        return

    LOGGER.info("Setting up SSH key for copy-offload.")

    # Get public key
    public_key = source_provider.get_ssh_public_key()

    # Get datastore name
    datastore_id = copyoffload_cfg.get("datastore_id")
    if not datastore_id:
        pytest.fail("datastore_id is required in copyoffload config for SSH method.")
    datastore_name = source_provider.get_datastore_name_by_id(datastore_id)

    # Get ESXi credentials from the 'copyoffload' config section
    esxi_host = copyoffload_cfg.get("esxi_host")
    esxi_user = copyoffload_cfg.get("esxi_user")
    esxi_password = copyoffload_cfg.get("esxi_password")

    if not all([esxi_host, esxi_user, esxi_password]):
        pytest.fail(
            "esxi_host, esxi_user, and esxi_password are required in the 'copyoffload' section of provider config for SSH method."
        )

    # Install the key
    install_ssh_key_on_esxi(
        host=esxi_host,
        username=esxi_user,
        password=esxi_password,
        public_key=public_key,
        datastore_name=datastore_name,
    )

    yield

    # Teardown: Remove the key
    LOGGER.info("Tearing down SSH key for copy-offload.")
    remove_ssh_key_from_esxi(
        host=esxi_host,
        username=esxi_user,
        password=esxi_password,
        public_key=public_key,
    )
