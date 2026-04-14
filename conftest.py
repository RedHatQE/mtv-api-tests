from __future__ import annotations

import json
import logging
import os
import pickle
import shutil
import tempfile
import uuid
from collections.abc import Generator
from copy import deepcopy
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, Any

import filelock
import pytest
from kubernetes.dynamic.exceptions import ForbiddenError, NotFoundError

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.node import Node
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from ocp_resources.secret import Secret
from ocp_resources.storage_class import StorageClass
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.subscription import Subscription
from ocp_resources.virtual_machine import VirtualMachine
from pytest_harvest import get_fixture_store
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutSampler

from exceptions.exceptions import (
    ForkliftPodsNotRunningError,
    MissingProvidersFileError,
    MtvOperatorNotInstalledError,
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
from libs.providers.vmware import VMWareProvider
from utilities.constants import MTV_OPERATOR_NAME
from utilities.hooks import create_hook_if_configured
from utilities.logger import separator, setup_logging
from utilities.mtv_migration import get_vm_suffix
from utilities.must_gather import run_must_gather
from utilities.naming import generate_name_with_uuid, sanitize_test_name_for_path
from utilities.pytest_utils import (
    collect_created_resources,
    enrich_junit_xml,
    is_dry_run,
    prepare_base_path,
    session_teardown,
    setup_ai_analysis,
)
from utilities.resources import create_and_store_resource, get_or_create_namespace
from utilities.ssh_utils import SSHConnectionManager
from utilities.utils import (
    create_source_cnv_vms,
    create_source_provider,
    extract_vm_from_plan,
    generate_class_hash_prefix,
    get_cluster_client,
    get_cluster_version,
    get_cluster_version_str,
    get_value_from_py_config,
    load_source_providers,
    resolve_providers_json_path,
)
from utilities.virtctl import add_to_path, download_virtctl_from_cluster
from utilities.vmware_guest_operations import detect_vmware_ip_origins_via_guest_ops
from utilities.worker_node_selection import get_worker_nodes, select_node_by_available_memory

RESULTS_PATH = Path("./.xdist_results/")
RESULTS_PATH.mkdir(exist_ok=True)
LOGGER = logging.getLogger(__name__)
BASIC_LOGGER = logging.getLogger("basic")


# Pytest start


def pytest_addoption(parser):
    data_collector_group = parser.getgroup(name="DataCollector")
    teardown_group = parser.getgroup(name="Teardown")
    openshift_python_wrapper_group = parser.getgroup(name="Openshift Python Wrapper")
    analyze_with_ai_group = parser.getgroup(name="Analyze with AI")
    analyze_with_ai_group.addoption("--analyze-with-ai", action="store_true", help="Analyze test failures using AI")

    providers_group = parser.getgroup(name="Providers")
    providers_group.addoption(
        "--providers-json",
        help="Path to providers JSON configuration file. Falls back to PROVIDERS_JSON_PATH env var, then .providers.json",
        default=None,
    )

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


def pytest_configure(config: pytest.Config) -> None:
    """Set a unique basetemp directory per session to prevent cross-session conflicts.

    Args:
        config (pytest.Config): Pytest configuration object used to set basetemp.
    """
    if config.option.basetemp is None:
        config.option.basetemp = str(Path(tempfile.gettempdir()) / f"pytest-{uuid.uuid4().hex[:8]}")


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
    required_config = ("storage_class", "source_provider")

    if not is_dry_run(session.config):
        BASIC_LOGGER.info(f"{separator(symbol_='-', val='SESSION START')}")

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

    if session.config.getoption("analyze_with_ai"):
        setup_ai_analysis(session)


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
    if is_dry_run(session.config):
        return

    BASIC_LOGGER.info(f"{separator(symbol_='-', val='SESSION FINISH')}")

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

    if session.config.getoption("analyze_with_ai"):
        if exitstatus == 0:
            LOGGER.info("No test failures (exit code %d), skipping AI analysis", exitstatus)

        else:
            try:
                enrich_junit_xml(session)
            except Exception:
                LOGGER.exception("Failed to enrich JUnit XML, original preserved")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    # -------------------------------------------------------------------
    # Provider-type based test skipping at collection time.
    #
    # This section conditionally skips or marks tests based on the source
    # provider type (e.g., vSphere, RHV, OpenStack). It runs during
    # pytest collection so that incompatible tests are excluded before
    # execution begins.
    #
    # Why here (not at module level): The provider JSON path comes from
    # the --providers-json CLI arg, which is only available via `config`
    # inside pytest hooks.
    #
    # How to add a new skip rule:
    #   1. Register the marker in pytest.ini under [pytest] > markers.
    #   2. Add a condition block below following the existing pattern:
    #        - Check source_provider_type against Provider.ProviderType.*
    #        - Create a skip/jira marker with a descriptive reason
    #        - Iterate over items and add the marker where the keyword matches
    #   3. Apply the marker to the relevant test class, e.g.:
    #        @pytest.mark.my_new_marker
    #        class TestMyFeature: ...
    # -------------------------------------------------------------------
    if not is_dry_run(config):
        providers_json_path = config.getoption("providers_json", default=None)
        providers = load_source_providers(providers_json_path=providers_json_path)
        # .get() with default is intentional: source_provider may not be configured (e.g., partial config),
        # in which case provider-type gating is silently skipped.
        source_provider_type = providers.get(py_config.get("source_provider", ""), {}).get("type")

        if source_provider_type:
            # Skip warm migration tests for providers that do not support warm migration.
            warm_unsupported = (
                Provider.ProviderType.OPENSTACK,
                Provider.ProviderType.OPENSHIFT,
                Provider.ProviderType.OVA,
            )
            if source_provider_type in warm_unsupported:
                warm_skip = pytest.mark.skip(reason=f"{source_provider_type} warm migration is not supported.")
                for item in items:
                    if "warm" in item.keywords:
                        item.add_marker(warm_skip)

            # Mark RHV warm migration tests with a Jira issue (disables execution until resolved).
            if source_provider_type == Provider.ProviderType.RHV:
                for item in items:
                    if "warm" in item.keywords:
                        item.add_marker(pytest.mark.jira("MTV-2846", run=False))

            # Skip copy-offload tests for non-vSphere providers (vSphere-only feature).
            if source_provider_type != Provider.ProviderType.VSPHERE:
                copyoffload_skip = pytest.mark.skip(
                    reason="Copy-offload tests are only applicable to vSphere source providers"
                )
                for item in items:
                    if "copyoffload" in item.keywords:
                        item.add_marker(copyoffload_skip)

    _session_store = get_fixture_store(session)
    vms_for_current_session: set = set()

    for item in items:
        item.name = f"{item.name}-{py_config.get('source_provider')}-{py_config.get('storage_class')}"

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

    if not is_dry_run(session.config):
        LOGGER.info(f"Base VMS names for current session:\n {'\n'.join(vms_for_current_session)}")


def pytest_exception_interact(node, call, report):
    if is_dry_run(node.session.config):
        return

    if not node.session.config.getoption("skip_data_collector"):
        _session_store = get_fixture_store(node.session)
        _data_collector_path = Path(
            f"{node.session.config.getoption('data_collector_path')}/{sanitize_test_name_for_path(node.name)}"
        )
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
def source_providers(request: pytest.FixtureRequest) -> dict[str, dict[str, Any]]:
    providers_json_path = request.config.getoption("providers_json")
    return load_source_providers(providers_json_path=providers_json_path)


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
    cluster_version_str = get_cluster_version_str(ocp_admin_client)

    # Persistent shared directory for virtctl binary caching:
    # - Path is intentionally persistent across test runs (not session-scoped tmp)
    # - Visible to all pytest-xdist workers for cross-worker caching
    # - Avoids re-downloading virtctl on every test session
    # - Includes cluster version for automatic cache invalidation
    # - Do NOT change to pytest's tmp_path or similar session-scoped directories
    shared_dir = Path(tempfile.gettempdir()) / "pytest-shared-virtctl" / cluster_version_str.replace(".", "-")
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
def precopy_interval_forkliftcontroller(
    ocp_admin_client: "DynamicClient",
    mtv_namespace: str,
) -> Generator[None, None, None]:
    """Set the precopy interval in ForkliftController if not already configured.

    Uses check-and-set without ResourceEditor context manager to avoid
    restoring the original value on session teardown. This prevents race
    conditions during parallel execution where one worker's teardown would
    clobber the precopy interval for other workers still running warm migrations.
    """
    forklift_controller = ForkliftController(
        client=ocp_admin_client,
        name="forklift-controller",
        namespace=mtv_namespace,
        ensure_exists=True,
    )

    snapshots_interval = str(py_config["snapshots_interval"])

    forklift_controller.wait_for_condition(
        status=forklift_controller.Condition.Status.TRUE,
        condition=forklift_controller.Condition.Type.RUNNING,
        timeout=300,
    )

    current_interval = getattr(forklift_controller.instance.spec, "controller_precopy_interval", None)

    if str(current_interval) == snapshots_interval:
        LOGGER.info(
            f"ForkliftController controller_precopy_interval already set to {snapshots_interval}, skipping update"
        )
        yield
        return

    LOGGER.info(
        f"Setting ForkliftController controller_precopy_interval from {current_interval!r} to {snapshots_interval}"
    )
    ResourceEditor(patches={forklift_controller: {"spec": {"controller_precopy_interval": snapshots_interval}}}).update(
        backup_resources=False
    )

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
def source_provider_data(
    source_providers: dict[str, dict[str, Any]],
    fixture_store: dict[str, Any],
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    """Resolve source provider configuration from the loaded providers data.

    Args:
        source_providers (dict[str, dict[str, Any]]): All provider configurations loaded from providers JSON file.
        fixture_store (dict[str, Any]): Session fixture store for teardown tracking.
        request (pytest.FixtureRequest): Pytest request object to access CLI options.

    Returns:
        dict[str, Any]: The resolved source provider configuration dict.

    Raises:
        MissingProvidersFileError: If providers data is empty.
        ValueError: If the requested provider is not found.
    """
    providers_path = resolve_providers_json_path(cli_path=request.config.getoption("providers_json"))

    if not source_providers:
        raise MissingProvidersFileError(path=providers_path)

    requested_provider = py_config["source_provider"]
    if requested_provider not in source_providers:
        raise ValueError(
            f"Source provider '{requested_provider}' not found in '{providers_path}'. "
            f"Available providers: {sorted(source_providers.keys())}"
        )

    _source_provider = source_providers[requested_provider]
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
def vcenter_clone_provider(
    source_provider_data: dict[str, Any],
    source_providers: dict[str, dict[str, Any]],
    fixture_store: dict[str, Any],
) -> Generator[VMWareProvider | None, None, None]:
    """Create a VMWareProvider for VM cloning when source provider can't clone directly.

    Used for ESXi providers that reference a vCenter via 'clone_provider' config field.
    The vCenter handles CloneVM_Task operations that ESXi doesn't support.

    Args:
        source_provider_data: Source provider configuration.
        source_providers: All provider configurations from providers JSON.
        fixture_store: Session fixture store for resource tracking.

    Yields:
        VMWareProvider | None: vCenter provider for cloning, or None if not configured.

    Raises:
        ValueError: If clone_provider references a non-existent or non-vSphere provider.
    """
    clone_provider_name = source_provider_data.get("clone_provider")
    if not clone_provider_name:
        yield None
        return

    if clone_provider_name not in source_providers:
        raise ValueError(
            f"clone_provider '{clone_provider_name}' not found in providers JSON. "
            f"Available providers: {sorted(source_providers.keys())}"
        )

    clone_provider_data = source_providers[clone_provider_name]
    if clone_provider_data["type"] != "vsphere":
        raise ValueError(
            f"clone_provider '{clone_provider_name}' must be type 'vsphere', got '{clone_provider_data['type']}'"
        )

    fixture_store["clone_provider_data"] = clone_provider_data

    LOGGER.info(f"Creating vCenter clone provider '{clone_provider_name}' for VM cloning")
    provider = VMWareProvider(
        host=clone_provider_data["fqdn"],
        username=clone_provider_data["username"],
        password=clone_provider_data["password"],
        fixture_store=fixture_store,
    )
    provider.connect()
    try:
        yield provider
    finally:
        provider.disconnect()


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
) -> dict[str, str]:
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
        dict[str, str]: Dictionary with "name" (base name) and "namespace" (NAD namespace)
    """
    hash_prefix = generate_class_hash_prefix(request.node.nodeid, session_uuid=fixture_store["session_uuid"])
    base_name = f"cb-{hash_prefix}"

    class_name = request.node.cls.__name__ if request.node.cls else request.node.name
    LOGGER.info(f"Creating class-scoped NADs with base name: {base_name} (class: {class_name})")

    # Check for custom multus namespace in plan config
    multus_namespace = class_plan_config.get("multus_namespace")
    if multus_namespace:
        LOGGER.info(f"Using custom multus namespace: {multus_namespace}")
        nad_namespace = get_or_create_namespace(
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            namespace_name=multus_namespace,
        )
    else:
        nad_namespace = target_namespace

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
            namespace=nad_namespace,
            config=config,
            name=nad_name,
        )

        created_nads.append(nad_name)
        LOGGER.info(f"Created NAD: {nad_name} in namespace {nad_namespace}")

    LOGGER.info(f"Created {len(created_nads)} class-scoped NADs: {created_nads}")

    # Return dict with base name and namespace
    return {"name": base_name, "namespace": nad_namespace}


@pytest.fixture(scope="class")
def vm_ssh_connections(fixture_store, destination_provider, prepared_plan, ocp_admin_client):
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
    vm_namespace = prepared_plan["_vm_target_namespace"]
    manager = SSHConnectionManager(
        provider=destination_provider,
        namespace=vm_namespace,
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
    target_namespace: str,
    vcenter_clone_provider: VMWareProvider | None,
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
        target_namespace (str): Default target namespace for VMs
        vcenter_clone_provider (VMWareProvider | None): vCenter provider for cloning, or None

    Yields:
        dict[str, Any]: Prepared plan with updated VM names
    """

    # Deep copy the plan config to avoid mutation
    plan: dict[str, Any] = deepcopy(class_plan_config)
    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
    warm_migration = plan.get("warm_migration", False)

    # Initialize separate storage for source VM data (keeps virtual_machines clean for Plan CR serialization)
    plan["source_vms_data"] = {}

    # Handle custom VM target namespace
    vm_target_namespace = plan.get("vm_target_namespace")
    if vm_target_namespace:
        LOGGER.info(f"Using custom VM target namespace: {vm_target_namespace}")
        get_or_create_namespace(
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            namespace_name=vm_target_namespace,
        )
        plan["_vm_target_namespace"] = vm_target_namespace
    else:
        plan["_vm_target_namespace"] = target_namespace

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
            hash_prefix = generate_class_hash_prefix(request.node.nodeid, session_uuid=fixture_store["session_uuid"])
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

        # Use vCenter clone provider if configured (e.g., for ESXi sources that can't clone directly)
        clone_provider = vcenter_clone_provider or source_provider

        for vm in virtual_machines:
            clone_options = {**vm, "enable_ctk": warm_migration}
            provider_vm_api = clone_provider.get_vm_by_name(
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
                    source_provider.wait_for_vmware_guest_info(
                        provider_vm_api, timeout=class_plan_config.get("guest_agent_timeout", 120)
                    )
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

            # Detect IP origins via Guest Operations for Linux VMs where VMware doesn't report origin
            # (known open-vm-tools limitation: https://github.com/vmware/open-vm-tools/issues/694)
            if source_provider.type == Provider.ProviderType.VSPHERE and not source_vm_details.get("win_os"):
                try:
                    detect_vmware_ip_origins_via_guest_ops(
                        source_provider=source_provider,
                        vm=provider_vm_api,
                        source_provider_data=fixture_store["source_provider_data"],
                        vm_details=source_vm_details,
                    )
                except ValueError:
                    raise
                except Exception as e:
                    if plan.get("preserve_static_ips"):
                        raise ValueError(
                            f"Failed to detect IP origins via Guest Operations for VM {vm['name']}: {e}. "
                            "IP origin detection is required when preserve_static_ips is enabled."
                        ) from e
                    LOGGER.warning(
                        f"Failed to detect IP origins via Guest Operations for VM {vm['name']}: {e}. "
                        "Static IP verification may not work for this VM."
                    )

    # Create Hooks if configured
    create_hook_if_configured(plan, "pre_hook", "pre", fixture_store, ocp_admin_client, target_namespace)
    create_hook_if_configured(plan, "post_hook", "post", fixture_store, ocp_admin_client, target_namespace)

    yield plan

    # Note: VMs are cleaned up by cleanup_migrated_vms at class scope.
    # Session-level registration is intentionally omitted to prevent double cleanup.
    # The cleanup_migrated_vms fixture handles VM deletion, and any leftover resources
    # (e.g., if cleanup_migrated_vms is not used) will be caught by namespace deletion.


@pytest.fixture(scope="class")
def prepared_plan_1(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    """Prepare first migration plan configuration for simultaneous migrations.

    This fixture extracts the first VM from a prepared plan containing multiple VMs,
    creating an independent plan for the first migration. Use this with prepared_plan_2
    to run two migrations simultaneously.

    Args:
        prepared_plan: Base prepared plan with cloned VMs

    Returns:
        Deep copy of prepared plan with first VM only
    """
    return extract_vm_from_plan(prepared_plan, vm_index=0, fixture_name="prepared_plan_1")


@pytest.fixture(scope="class")
def prepared_plan_2(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    """Prepare second migration plan configuration for simultaneous migrations.

    This fixture extracts the second VM from a prepared plan containing multiple VMs,
    creating an independent plan for the second migration. Use this with prepared_plan_1
    to run two migrations simultaneously.

    Args:
        prepared_plan: Base prepared plan with cloned VMs

    Returns:
        Deep copy of prepared plan with second VM only
    """
    return extract_vm_from_plan(prepared_plan, vm_index=1, fixture_name="prepared_plan_2")


@pytest.fixture(scope="class")
def mtv_version_checker(request: pytest.FixtureRequest, ocp_admin_client: DynamicClient) -> None:
    """Check if test requires minimum MTV version and skip if not met.

    Args:
        request: pytest request object containing test markers
        ocp_admin_client: OpenShift DynamicClient for version queries

    Usage:
        @pytest.mark.usefixtures("mtv_version_checker")
        @pytest.mark.min_mtv_version("2.10.0")
        def test_something(...):
            # Test runs only if MTV >= 2.10.0

    Raises:
        pytest.skip: If MTV version doesn't meet minimum

    Returns:
        None: This fixture performs validation only.
    """
    marker = request.node.get_closest_marker("min_mtv_version")
    if marker:
        min_version = marker.args[0]
        from utilities.utils import has_mtv_minimum_version  # noqa: PLC0415

        if not has_mtv_minimum_version(min_version, client=ocp_admin_client):
            pytest.skip(f"Test requires MTV {min_version}+")


@pytest.fixture(scope="class")
def labeled_worker_node(
    prepared_plan: dict[str, Any],
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
) -> Generator[dict[str, str], None, None]:
    """Label a worker node for target scheduling tests.

    Uses Prometheus to select node with most available memory.
    Applies label using ResourceEditor context manager for automatic cleanup.

    Args:
        prepared_plan: Test plan configuration containing target_node_selector
        ocp_admin_client: OpenShift DynamicClient for node operations
        fixture_store: Fixture store containing session_uuid

    Returns:
        dict with keys: node_name, label_key, label_value

    Raises:
        ValueError: If target_node_selector not in test config, does not contain exactly one label,
            or no worker nodes found
    """
    try:
        target_node_selector = prepared_plan["target_node_selector"]
    except KeyError:
        raise ValueError(
            "target_node_selector not found in test configuration. "
            "Add 'target_node_selector' to your test config in tests/tests_config/config.py"
        ) from None

    if len(target_node_selector) != 1:
        raise ValueError(f"target_node_selector must contain exactly one label, got {len(target_node_selector)}")

    worker_nodes = get_worker_nodes(ocp_admin_client)
    if not worker_nodes:
        raise ValueError("No worker nodes found in cluster")

    target_node = select_node_by_available_memory(ocp_admin_client, worker_nodes)

    # Extract base label key and configured value
    base_label_key, config_value = next(iter(target_node_selector.items()))

    # When config_value is None (auto-generated mode), make key unique per session
    # to prevent parallel execution conflicts (two sessions on same node would overwrite each other's label)
    if config_value is None:
        session_uuid = fixture_store["session_uuid"]
        suffix = f"-{session_uuid}"
        # For qualified keys (prefix/name), only truncate the name segment
        if "/" in base_label_key:
            prefix, name = base_label_key.split("/", 1)
            label_key = f"{prefix}/{name[: 63 - len(suffix)]}{suffix}"
        else:
            label_key = f"{base_label_key[: 63 - len(suffix)]}{suffix}"
        label_value = session_uuid
    else:
        label_key = base_label_key
        label_value = config_value

    LOGGER.info(f"Labeling node '{target_node}' with {label_key}={label_value} for target scheduling")

    # Apply label with automatic cleanup via context manager
    node = Node(client=ocp_admin_client, name=target_node)
    with ResourceEditor(patches={node: {"metadata": {"labels": {label_key: label_value}}}}):
        yield {
            "node_name": target_node,
            "label_key": label_key,
            "label_value": label_value,
        }


@pytest.fixture(scope="class")
def target_vm_labels(prepared_plan: dict[str, Any], fixture_store: dict[str, Any]) -> dict[str, Any]:
    """Generate VM labels for targetLabels testing.

    Supports auto-generation: if label value is None in config, replaces with session_uuid.

    Args:
        prepared_plan: Test plan configuration
        fixture_store: Fixture store containing session_uuid

    Returns:
        dict with "vm_labels" key containing label dict

    Raises:
        ValueError: If target_labels not in test config
    """
    try:
        target_labels = prepared_plan["target_labels"]
    except KeyError:
        raise ValueError(
            "target_labels not found in test configuration. "
            "Add 'target_labels' to your test config in tests/tests_config/config.py"
        ) from None
    session_uuid = fixture_store["session_uuid"]

    vm_labels = {}
    for label_key, config_value in target_labels.items():
        # None means auto-generate using session_uuid, otherwise use provided value
        label_value = session_uuid if config_value is None else config_value
        vm_labels[label_key] = label_value

    LOGGER.info(f"Generated VM labels: {vm_labels}")
    return {"vm_labels": vm_labels}


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

    # Use custom namespace if configured, otherwise fall back to target_namespace
    vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)

    for vm in prepared_plan["virtual_machines"]:
        vm_name = vm["name"]
        vm_obj = VirtualMachine(
            client=ocp_admin_client,
            name=vm_name,
            namespace=vm_namespace,
        )
        if vm_obj.exists:
            LOGGER.info(f"Cleaning up migrated VM: {vm_name} from namespace: {vm_namespace}")
            vm_obj.clean_up()
        else:
            LOGGER.info(f"VM {vm_name} already deleted from namespace: {vm_namespace}, skipping cleanup")


@pytest.fixture(scope="session")
def forklift_pods_state(ocp_admin_client: DynamicClient) -> None:
    mtv_namespace: str = py_config["mtv_namespace"]
    try:
        mtv_subscription = Subscription(
            client=ocp_admin_client,
            name=MTV_OPERATOR_NAME,
            namespace=mtv_namespace,
        )
        if not mtv_subscription.exists:
            raise MtvOperatorNotInstalledError(namespace=mtv_namespace)
    except ForbiddenError:
        LOGGER.warning(
            f"Insufficient RBAC permissions to check MTV Subscription in namespace '{mtv_namespace}'. "
            "Falling back to pod-based readiness check."
        )
    except NotFoundError:
        raise MtvOperatorNotInstalledError(namespace=mtv_namespace)

    def _get_not_running_pods(_admin_client: DynamicClient) -> bool:
        controller_pod: Pod | None = None
        not_running_pods: list[str] = []

        for pod in Pod.get(client=_admin_client, namespace=mtv_namespace):
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
