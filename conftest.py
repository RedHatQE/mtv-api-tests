from __future__ import annotations

import json
import logging
import os
import pickle
import shutil
import tempfile
from collections.abc import Generator
from copy import deepcopy
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, Any

import filelock
import pytest
from kubernetes.dynamic.exceptions import NotFoundError

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
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
from utilities.copyoffload_constants import SUPPORTED_VENDORS
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
from utilities.ssh_utils import SSHConnectionManager
from utilities.utils import (
    create_source_cnv_vms,
    create_source_provider,
    generate_class_hash_prefix,
    get_cluster_client,
    get_cluster_version,
    get_value_from_py_config,
)
from utilities.virtctl import add_to_path, download_virtctl_from_cluster

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

    # Incremental test support - track failures for class-based tests
    if "incremental" in item.keywords and rep.when == "call" and rep.failed:
        item.parent._previousfailed = item


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
    # Incremental test support - xfail if previous test in class failed
    if "incremental" in item.keywords:
        previousfailed = getattr(item.parent, "_previousfailed", None)
        if previousfailed is not None:
            pytest.xfail(f"previous test failed ({previousfailed.name})")

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

        # Get test config from parametrization or tests_params
        test_config = None
        if hasattr(item, "callspec"):
            # Class-based tests use class_plan_config
            test_config = item.callspec.params.get("class_plan_config")
            if test_config is None:
                # Function-based tests use plan
                test_config = item.callspec.params.get("plan")

        if test_config is None:
            # Fallback to looking up by test name (for non-parametrized tests)
            test_config = py_config["tests_params"].get(item.originalname)

        if test_config and "virtual_machines" in test_config:
            for _vm in test_config["virtual_machines"]:
                vms_for_current_session.add(_vm["name"])

    _session_store["vms_for_current_session"] = vms_for_current_session

    if not (session.config.getoption("--setupplan") or session.config.getoption("--collectonly")):
        LOGGER.info(f"Base VMS names for current session:\n {'\n'.join(vms_for_current_session)}")


def pytest_exception_interact(node, call, report):
    if not node.session.config.getoption("skip_data_collector"):
        _session_store = get_fixture_store(node.session)
        _data_collector_path = Path(f"{node.session.config.getoption('data_collector_path')}/{node.name}")
        # Handle both function-based tests and class-based tests
        test_name = node._pyfuncitem.name if hasattr(node, "_pyfuncitem") else node.name
        plans = _session_store["teardown"].get("Plan", [])
        plan = [plan for plan in plans if plan.get("test_name", "") == test_name]
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
def virtctl_binary(ocp_admin_client: "DynamicClient") -> Path:
    """Download and configure virtctl binary from the cluster.

    This fixture ensures virtctl is available in PATH for all tests
    that need to interact with VMs via virtctl commands.

    Uses file locking to handle pytest-xdist parallel execution safely.
    The binary is downloaded to a shared directory that all workers can access.
    The directory includes the cluster version for automatic cache invalidation
    when switching between clusters with different versions.

    Args:
        ocp_admin_client (DynamicClient): OpenShift cluster client for accessing
            the cluster to download the virtctl binary.

    Returns:
        Path: Path to the downloaded virtctl binary.

    Raises:
        ValueError: If virtctl download fails or binary is not executable.
        PermissionError: If shared directory ownership doesn't match current user.
        PermissionError: If shared directory is a symlink (hijack attempt).
        TimeoutError: If timeout waiting for file lock.
    """
    # Get cluster version for versioned caching
    cluster_version = get_cluster_version(ocp_admin_client)

    # Persistent shared directory for virtctl binary caching:
    # - Path is intentionally persistent across test runs (not session-scoped tmp)
    # - Visible to all pytest-xdist workers for cross-worker caching
    # - Avoids re-downloading virtctl on every test session
    # - Includes cluster version for automatic cache invalidation
    # - Do NOT change to pytest's tmp_path or similar session-scoped directories
    shared_dir = Path(tempfile.gettempdir()) / "pytest-shared-virtctl" / str(cluster_version)
    shared_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Security: check for symlink hijack attack
    if shared_dir.is_symlink():
        raise PermissionError(
            f"Security error: shared directory {shared_dir} is a symlink. This may indicate a hijack attempt."
        )

    # Security: verify ownership and enforce permissions
    current_uid = os.getuid()
    dir_stat = shared_dir.lstat()
    if dir_stat.st_uid != current_uid:
        raise PermissionError(
            f"Security error: shared directory {shared_dir} is owned by uid {dir_stat.st_uid}, "
            f"expected current user uid {current_uid}. This may indicate a hijack attempt."
        )
    os.chmod(shared_dir, 0o700)

    lock_file = shared_dir / "virtctl.lock"
    virtctl_path = shared_dir / "virtctl"

    try:
        # File lock ensures only one process downloads
        with filelock.FileLock(lock_file, timeout=600):
            if not virtctl_path.is_file() or not os.access(virtctl_path, os.X_OK):
                download_virtctl_from_cluster(client=ocp_admin_client, download_dir=shared_dir)
                # Validate binary was downloaded successfully
                if not virtctl_path.is_file() or not os.access(virtctl_path, os.X_OK):
                    raise ValueError(f"Failed to download or make executable virtctl at {virtctl_path}")
    except filelock.Timeout as err:
        raise TimeoutError(
            f"Timeout (600s) waiting for virtctl lock at {lock_file}. Another process may be stuck."
        ) from err

    # Add to PATH for all workers
    add_to_path(str(shared_dir))
    return virtctl_path


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


@pytest.fixture(scope="class")
def multus_network_name(
    fixture_store: dict[str, Any],
    target_namespace: str,
    ocp_admin_client: DynamicClient,
    multus_cni_config: str,
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    class_plan_config: dict[str, Any],
    request: pytest.FixtureRequest,
) -> str:
    """Create NADs based on network requirements with unique names per test class.

    Automatically detects number of networks and creates NADs with class-unique naming:
    - cb-{6-char-hash}-1, cb-{6-char-hash}-2, etc. (e.g., "cb-a1b2c3-1" = 11 chars)

    Uses SHA-256 (FIPS-compliant) instead of MD5 for hash generation.
    The unique class hash prevents conflicts when running tests in parallel.
    Names are kept under 15 characters to comply with Linux bridge interface name limits.

    Args:
        fixture_store (dict[str, Any]): Fixture store for resource tracking
        target_namespace (str): Target namespace for NADs
        ocp_admin_client (DynamicClient): OpenShift client
        multus_cni_config (str): Multus CNI configuration
        source_provider (BaseProvider): Source provider instance
        source_provider_inventory (ForkliftInventory): Source provider inventory
        class_plan_config (dict[str, Any]): Plan configuration from class parametrization
        request (pytest.FixtureRequest): Pytest fixture request

    Returns:
        str: Base name for NAD generation (e.g., "cb-a1b2c3")
    """
    hash_prefix = generate_class_hash_prefix(request.node.nodeid)
    base_name = f"cb-{hash_prefix}"

    class_name = request.node.cls.__name__ if request.node.cls else request.node.name
    LOGGER.info(f"Creating class-scoped NADs with base name: {base_name} (class: {class_name})")

    # Get VM/template names from the class plan config
    vms = [vm["name"] for vm in class_plan_config["virtual_machines"]]
    LOGGER.info(f"Found VMs from class config: {vms}")

    # Query networks using provider abstraction (handles templates vs VMs internally)
    networks = source_provider.get_vm_or_template_networks(names=vms, inventory=source_provider_inventory)

    if not networks:
        raise ValueError(f"No networks found for VMs {vms}. VMs must have at least one network interface.")

    # Calculate how many multus NADs we need (all networks except the first one)
    multus_count = max(0, len(networks) - 1)  # First network goes to pod, rest to multus

    created_nads = []
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

    LOGGER.info(f"Created {len(created_nads)} class-scoped NADs: {created_nads}")

    # Return the base name - consuming code will generate the same indexed names
    # This maintains the contract: fixture creates NADs, returns base name for generation
    return base_name


@pytest.fixture(scope="class")
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


@pytest.fixture(scope="class")
def class_plan_config(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Get plan configuration for class-based tests.

    Args:
        request (pytest.FixtureRequest): Pytest fixture request

    Returns:
        dict[str, Any]: Plan configuration from test parametrization
    """
    return request.param


@pytest.fixture(scope="class")
def prepared_plan(
    request: pytest.FixtureRequest,
    class_plan_config: dict[str, Any],
    fixture_store: dict[str, Any],
    source_provider: Any,
    source_vms_namespace: str,
    ocp_admin_client: DynamicClient,
    multus_cni_config: str,
    source_provider_inventory: ForkliftInventory,
) -> Generator[dict[str, Any], None, None]:
    """Prepare plan with cloned VMs for class-based tests.

    This fixture handles VM cloning and name updates, similar to the
    function-scoped `plan` fixture but at class scope. It prepares VMs
    once per test class rather than once per test function.

    Args:
        request (pytest.FixtureRequest): Pytest fixture request
        class_plan_config (dict[str, Any]): Plan configuration from parametrization
        fixture_store (dict[str, Any]): Fixture store for resource tracking
        source_provider: Source provider instance (VMWareProvider, OvirtProvider, etc.)
        source_vms_namespace (str): Source VMs namespace
        ocp_admin_client (DynamicClient): OpenShift client
        multus_cni_config (str): Multus CNI configuration
        source_provider_inventory (ForkliftInventory): Source provider inventory

    Yields:
        dict[str, Any]: Prepared plan with updated VM names
    """

    # Deep copy the plan config to avoid mutation
    plan: dict[str, Any] = deepcopy(class_plan_config)
    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
    warm_migration = plan.get("warm_migration", False)

    # Initialize separate storage for source VM data (keeps virtual_machines clean for Plan CR serialization)
    plan["source_vms_data"] = {}

    # Override VM names from provider config if specified
    if hasattr(source_provider, "copyoffload_config") and source_provider.copyoffload_config:
        default_vm_override = source_provider.copyoffload_config.get("default_vm_name")
        if default_vm_override:
            for vm in virtual_machines:
                if vm.get("clone", False):  # Only override for cloned VMs
                    LOGGER.info(f"Overriding VM name '{vm['name']}' with '{default_vm_override}' from provider config")
                    vm["name"] = default_vm_override

    # OVA provider uses a fixed VM from the OVA file
    if source_provider.type == Provider.ProviderType.OVA:
        plan["virtual_machines"] = [{"name": "1nisim-rhel9-efi"}]

    if source_provider.type != Provider.ProviderType.OVA:
        openshift_source_provider: bool = source_provider.type == Provider.ProviderType.OPENSHIFT

        vm_name_suffix = get_vm_suffix(warm_migration=warm_migration)

        if openshift_source_provider:
            # Generate unique network name for class-based tests
            hash_prefix = generate_class_hash_prefix(request.node.nodeid)
            multus_network_name = f"cb-{hash_prefix}"

            # Create NAD for OpenShift source provider
            create_and_store_resource(
                fixture_store=fixture_store,
                resource=NetworkAttachmentDefinition,
                client=ocp_admin_client,
                namespace=source_vms_namespace,
                config=multus_cni_config,
                name=multus_network_name,
            )

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
            # Add enable_ctk flag for warm migrations
            clone_options = {**vm, "enable_ctk": warm_migration}
            provider_vm_api = source_provider.get_vm_by_name(
                query=vm["name"],
                vm_name_suffix=vm_name_suffix,
                clone_vm=True,
                session_uuid=fixture_store["session_uuid"],
                clone_options=clone_options,
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
            # Store complete source VM data separately (keeps virtual_machines clean for Plan CR serialization)
            plan["source_vms_data"][vm["name"]] = source_vm_details

    yield plan

    # Note: VMs are cleaned up by cleanup_migrated_vms at class scope.
    # Session-level registration is intentionally omitted to prevent double cleanup.
    # The cleanup_migrated_vms fixture handles VM deletion, and any leftover resources
    # (e.g., if cleanup_migrated_vms is not used) will be caught by namespace deletion.


@pytest.fixture(scope="class")
def cleanup_migrated_vms(
    request: pytest.FixtureRequest,
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    prepared_plan: dict[str, Any],
) -> Generator[None, None, None]:
    """Cleanup migrated VMs after test class completes.

    Teardown-only fixture that deletes VMs migrated during the test class.
    Honors --skip-teardown flag. Session teardown handles any leftovers.

    Args:
        request: Pytest fixture request for accessing config options
        ocp_admin_client: OpenShift client
        target_namespace: Namespace where VMs were migrated
        prepared_plan: Plan containing virtual_machines list

    Yields:
        None: Teardown-only fixture, no setup value

    Raises:
        Exception: If VM deletion fails
    """
    yield

    if request.config.getoption("skip_teardown"):
        LOGGER.info("Skipping VM cleanup due to --skip-teardown flag")
        return

    for vm in prepared_plan["virtual_machines"]:
        vm_name = vm["name"]
        vm_obj = VirtualMachine(
            client=ocp_admin_client,
            name=vm_name,
            namespace=target_namespace,
        )
        if vm_obj.exists:
            LOGGER.info(f"Cleaning up migrated VM: {vm_name}")
            vm_obj.clean_up()
        else:
            LOGGER.info(f"VM {vm_name} already deleted, skipping cleanup")


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
def multus_cni_config() -> str:
    bridge_type_and_name = "cnv-bridge"
    config = {"cniVersion": "0.3.1", "type": f"{bridge_type_and_name}", "bridge": f"{bridge_type_and_name}"}
    return json.dumps(config)


@pytest.fixture(scope="session")
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

    # Validate required copy-offload parameters
    required_params = ["storage_vendor_product", "datastore_id"]
    missing_params = [param for param in required_params if not config.get(param)]

    if missing_params:
        raise ValueError(
            f"Missing required copy-offload parameters in config: {', '.join(missing_params)}. "
            "Add them to .providers.json copyoffload section"
        )

    LOGGER.info("✓ Copy-offload configuration validated successfully")


@pytest.fixture(scope="class")
def mixed_datastore_config(source_provider_data: dict[str, Any]) -> None:
    """Validate mixed datastore configuration for TestCopyoffloadMixedDatastoreMigration.

    Args:
        source_provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        None

    Raises:
        ValueError: If non_xcopy_datastore_id is missing.
    """
    copyoffload_config_data: dict[str, Any] = source_provider_data.get("copyoffload", {})
    non_xcopy_datastore_id: str | None = copyoffload_config_data.get("non_xcopy_datastore_id")

    if not non_xcopy_datastore_id:
        raise ValueError(
            "Mixed datastore test requires 'non_xcopy_datastore_id' to be configured in copyoffload section. "
            "This should be a datastore that does NOT support XCOPY."
        )

    LOGGER.info(f"✓ Mixed datastore configuration validated: non_xcopy_datastore_id = {non_xcopy_datastore_id}")


@pytest.fixture(scope="session")
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
            f"storage_vendor_product is required in copyoffload configuration. "
            f"Valid values: {', '.join(SUPPORTED_VENDORS)}"
        )
    if storage_vendor not in SUPPORTED_VENDORS:
        LOGGER.warning(
            "storage_vendor_product '%s' is not in the list of known vendors: %s. "
            "Continuing anyway, but this may cause issues if the vendor is not supported by the populator.",
            storage_vendor,
            SUPPORTED_VENDORS,
        )

    # Base secret data (required for all vendors)
    secret_data = {
        "STORAGE_HOSTNAME": storage_hostname,
        "STORAGE_USERNAME": storage_username,
        "STORAGE_PASSWORD": storage_password,
    }

    # Vendor-specific configuration mapping
    # Maps vendor name to list of (config_key, secret_key, required) tuples
    # Based on forklift vsphere-xcopy-volume-populator code and README
    # NOTE: Keys must match SUPPORTED_VENDORS constant defined at module level
    vendor_specific_fields = {
        "ontap": [("ontap_svm", "ONTAP_SVM", True)],
        "vantara": [
            ("vantara_storage_id", "STORAGE_ID", True),
            ("vantara_storage_port", "STORAGE_PORT", True),
            ("vantara_hostgroup_id_list", "HOSTGROUP_ID_LIST", True),
        ],
        "primera3par": [],  # Only basic credentials required
        "pureFlashArray": [("pure_cluster_prefix", "PURE_CLUSTER_PREFIX", True)],
        "powerflex": [("powerflex_system_id", "POWERFLEX_SYSTEM_ID", True)],
        "powermax": [("powermax_symmetrix_id", "POWERMAX_SYMMETRIX_ID", True)],
        "powerstore": [],  # Only basic credentials required
        "infinibox": [],  # Only basic credentials required
        "flashsystem": [],  # Only basic credentials required
    }

    # Ensure vendor_specific_fields keys match SUPPORTED_VENDORS to prevent drift
    assert set(vendor_specific_fields.keys()) == set(SUPPORTED_VENDORS), (
        f"vendor_specific_fields keys must match SUPPORTED_VENDORS. "
        f"Missing in vendor_specific_fields: {set(SUPPORTED_VENDORS) - set(vendor_specific_fields.keys())}. "
        f"Extra in vendor_specific_fields: {set(vendor_specific_fields.keys()) - set(SUPPORTED_VENDORS)}"
    )

    # Add vendor-specific fields if configured
    if storage_vendor in vendor_specific_fields:
        for config_key, secret_key, required in vendor_specific_fields[storage_vendor]:
            value = get_copyoffload_credential(config_key, copyoffload_cfg)
            if value:
                secret_data[secret_key] = value
                LOGGER.info("✓ Added vendor-specific field: %s", secret_key)
            elif required:
                env_var_name = f"COPYOFFLOAD_{config_key.upper()}"
                raise ValueError(
                    f"Required vendor-specific field '{config_key}' not found for vendor '{storage_vendor}'. "
                    f"Add it to .providers.json copyoffload section or set environment variable: {env_var_name}"
                )

    LOGGER.info("Creating storage secret for copy-offload with vendor: %s", storage_vendor)

    storage_secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        string_data=secret_data,
    )

    LOGGER.info("✓ Copy-offload storage secret created: %s", storage_secret.name)
    return storage_secret


@pytest.fixture(scope="session")
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
    # These support environment variable overrides (COPYOFFLOAD_ESXI_HOST, etc.)
    esxi_host = get_copyoffload_credential("esxi_host", copyoffload_cfg)
    esxi_user = get_copyoffload_credential("esxi_user", copyoffload_cfg)
    esxi_password = get_copyoffload_credential("esxi_password", copyoffload_cfg)

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
