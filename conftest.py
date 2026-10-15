from __future__ import annotations

import bdb
import json
import logging
import os
import pickle
import shutil
import tempfile
import uuid
from collections.abc import Generator
from copy import deepcopy
from functools import partial
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
from pyVmomi import vim
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import (
    ForkliftPodsNotRunningError,
    GuestCommandError,
    MissingProvidersFileError,
    MtvOperatorNotInstalledError,
    RemoteClusterAndLocalCluterNamesError,
)
from libs.base_provider import _PROVIDER_TEST_MARKERS, BaseProvider, supports_add_nic
from libs.forklift_inventory import ForkliftInventory, create_forklift_inventory
from libs.providers.openshift import OCPProvider
from libs.providers.rhv import OvirtProvider
from libs.providers.vmware import VMWareProvider
from utilities.constants import MTV_OPERATOR_NAME
from utilities.copyoffload_constants import FORKLIFT_CONTROLLER_NAME
from utilities.copyoffload_migration import apply_copyoffload_vm_name_override, resolve_non_dedicated_esxi_host
from utilities.debug import remote_debugger_enabled, remote_post_mortem
from utilities.hooks import create_hook_if_configured
from utilities.logger import separator, setup_logging
from utilities.mtv_migration import get_vm_suffix, resolve_pvc_name_template
from utilities.must_gather import (
    collect_class_must_gather,
    collect_class_teardown_must_gather,
    collect_must_gather_for_item,
    collect_pending_class_must_gather_now,
    flush_pending_class_must_gathers,
    initialize_class_plan_context,
    mark_class_pending_must_gather,
    run_must_gather,
)
from utilities.naming import (
    generate_name_with_uuid,
    resolve_destination_vm_name,
    sanitize_kubernetes_name,
)
from utilities.provider_inventory import (
    get_missing_source_vms,
    raise_for_missing_source_vms,
    wait_for_added_nics_in_forklift_inventory,
    wait_for_cloned_vms_in_forklift_inventory,
    wait_for_inventory_guest_networks,
)
from utilities.provider_latency import select_provider_from_pool, store_provider_selection
from utilities.pytest_utils import (
    collect_created_resources,
    enrich_junit_xml,
    is_dry_run,
    prepare_base_path,
    resolve_item_plan_config,
    session_teardown,
    setup_ai_analysis,
)
from utilities.resources import create_and_store_resource, get_or_create_namespace
from utilities.ssh_utils import SSHConnectionManager
from utilities.storage_canary import verify_destination_storage_provisioning
from utilities.utils import (
    create_source_cnv_vms,
    create_source_provider,
    extract_vm_from_plan,
    generate_class_hash_prefix,
    get_cluster_client,
    get_cluster_version,
    get_cluster_version_str,
    get_per_nic_networks,
    get_value_from_py_config,
    load_source_providers,
    resolve_providers_json_path,
)
from utilities.virtctl import add_to_path, download_virtctl_from_cluster
from utilities.vmware_guest_operations import detect_guest_nic_names, detect_vmware_ip_origins_via_guest_ops
from utilities.worker_node_selection import get_worker_nodes, select_node_by_available_memory

RESULTS_PATH = Path("./.xdist_results/")
RESULTS_PATH.mkdir(exist_ok=True)
LOGGER = logging.getLogger(__name__)
BASIC_LOGGER = logging.getLogger("basic")
_STANDALONE_MUST_GATHER_ERROR = pytest.StashKey[Exception]()
_CLASS_MUST_GATHER_ERROR = pytest.StashKey[Exception]()


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


@pytest.fixture(scope="session")
def selected_source_provider(request: pytest.FixtureRequest, ocp_admin_client: DynamicClient) -> str:
    """Resolve the run's source provider, selecting the fastest pool member when requested.

    Explicit `source_provider` config is returned unchanged - nothing is probed and
    nothing changes. Otherwise the `source_provider_type` + `source_provider_version`
    pool (with optional `source_provider_feature`) is probed once per run using the
    session's admin client: in a single-process run the provider is selected inline,
    and under xdist the first worker to hold the run-scoped file lock probes and
    writes the selection while every other worker reads the same file, so all
    workers run against the same provider.

    Args:
        request (pytest.FixtureRequest): Request supplying the providers JSON option.
        ocp_admin_client (DynamicClient): Client used to run the probe pod.

    Returns:
        str: The explicitly configured or latency-selected source provider key.

    Raises:
        ValueError: When the pool is empty or has no reachable member.
    """
    if py_config.get("source_provider"):
        return py_config["source_provider"]

    select_from_pool = partial(
        select_provider_from_pool,
        client=ocp_admin_client,
        providers_json_path=request.config.getoption("providers_json"),
        provider_type=py_config["source_provider_type"],
        provider_version=py_config.get("source_provider_version"),
        required_feature=py_config.get("source_provider_feature"),
    )
    run_uid = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    if run_uid is None:
        selection = select_from_pool()
    else:
        # The testrunuid scopes the file to this run, so parallel runs never share it.
        # The first worker through the lock probes once; the rest read its selection.
        # A failed selection is recorded too, so the failure happens once instead of
        # once per worker, and workers see the original error instead of a lock timeout.
        selection_path = Path(tempfile.gettempdir()) / f"pytest-provider-selection-{run_uid}.json"
        # The lock timeout must cover one full probe cycle held by the first worker:
        # namespace deploy + Active wait, up to PROBE_POD_START_TIMEOUT_SECONDS of pod
        # start, PROBE_SCRIPT_TIMEOUT_SECONDS of exec, and the blocking namespace
        # cleanup — 900s leaves margin over that worst case.
        with filelock.FileLock(f"{selection_path}.lock", timeout=900):
            if selection_path.exists():
                selection = json.loads(selection_path.read_text())
                if "error" in selection:
                    raise ValueError(f"Provider selection failed on the first worker: {selection['error']}")
            else:
                try:
                    selection = select_from_pool()
                except (ValueError, TimeoutExpiredError) as selection_error:
                    store_provider_selection(selection_path=selection_path, content={"error": str(selection_error)})
                    raise
                store_provider_selection(selection_path=selection_path, content=selection)
    py_config["source_provider"] = selection["provider"]
    if selection["rtt_ms"] is None:
        LOGGER.info(f"Source provider for this run: {selection['provider']} (selected without probing)")
    else:
        LOGGER.info(f"Source provider for this run: {selection['provider']} ({selection['rtt_ms']:.1f} ms)")
    return selection["provider"]


def pytest_sessionstart(session):
    required_config = ("storage_class", "source_provider")

    if not is_dry_run(session.config):
        BASIC_LOGGER.info(f"{separator(symbol_='-', val='SESSION START')}")

        missing_configs: list[str] = []

        # Latency-based pool selection (issue #728): a provider-type pool key
        # (version optional) replaces the explicit provider key. The selection itself
        # runs later, in the selected_source_provider fixture, once per run.
        pool_type = py_config.get("source_provider_type")
        pool_version = py_config.get("source_provider_version")
        if not py_config.get("source_provider") and pool_version and not pool_type:
            pytest.exit(
                reason="source_provider_version requires source_provider_type",
                returncode=1,
            )

        for _req in required_config:
            if _req == "source_provider" and pool_type:
                # Pool mode: the provider is resolved by the latency fixture.
                continue
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


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, None, None]:
    """Initialize class-plan state before setup, including hooks that can skip or fail.

    Args:
        item (pytest.Item): Item whose setup/call/teardown protocol is starting.

    Returns:
        None: Hook wrapper — the yield passes control to the inner runtest protocol hooks.
    """
    if not is_dry_run(item.config) and not item.config.getoption("skip_data_collector"):
        initialize_class_plan_context(item)
    yield


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


@pytest.hookimpl(tryfirst=True, wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, object, object]:
    """Gather at a class-plan boundary, always allowing pytest's fixture finalization.

    Args:
        item (pytest.Item): Item about to tear down.
        nextitem (pytest.Item | None): Next worker item, or None when execution stops.

    Returns:
        object: The result of the inner pytest_runtest_teardown hooks, forwarded unchanged.
    """
    BASIC_LOGGER.info(f"{separator(symbol_='-', val='TEARDOWN')}")
    try:
        collector_error = item.stash.get(_STANDALONE_MUST_GATHER_ERROR, None)
        if collector_error is not None:
            raise collector_error.with_traceback(collector_error.__traceback__)
        if not is_dry_run(item.config) and not item.config.getoption("skip_data_collector"):
            try:
                collect_class_must_gather(item, nextitem)
            except Exception as exp:
                # Report the collector error once as this item's teardown error. The stash keeps
                # pytest_exception_interact from retrying the same gather inside its hook (a second
                # raise there becomes an INTERNALERROR); the identity stays pending for the
                # session-end flush.
                item.stash[_CLASS_MUST_GATHER_ERROR] = exp
                raise
    finally:
        teardown_outcome = yield
    return teardown_outcome


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


@pytest.hookimpl(tryfirst=True, wrapper=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> Generator[None, object, object]:
    """Flush diagnostics and finish cleanup without suppressing collector programming errors.

    Diagnostics flush, resource inventory, and configured teardown run before the yield: the xdist
    worker reports completion from its own sessionfinish wrapper, so cleanup placed after that
    yield could be interrupted by worker shutdown mid-deletion. Work that needs other plugins'
    sessionfinish output (JUnit enrichment, terminal summary) runs after the yield, even when an
    inner session-finish hook raises. An inner-hook, cleanup, or post-yield failure is re-raised
    after that processing, chained from the earliest earlier error so all of them stay visible.

    Args:
        session (pytest.Session): Worker-local session holding pending diagnostics and resources.
        exitstatus (int): Pytest exit status before session cleanup.

    Returns:
        object: The result of the inner pytest_sessionfinish hooks, forwarded unchanged.
    """
    if is_dry_run(session.config):
        return (yield)

    BASIC_LOGGER.info(f"{separator(symbol_='-', val='SESSION FINISH')}")

    _session_store = get_fixture_store(session)

    _data_collector_path = Path(session.config.getoption("data_collector_path"))

    # --- Pre-yield cleanup: must finish before the xdist worker reports completion ---
    cleanup_error: Exception | None = None
    inner_error: Exception | None = None
    try:
        # Each independent action runs even if an earlier one raises; errors stay chained.
        try:
            if not session.config.getoption("skip_data_collector"):
                flush_pending_class_must_gathers(session)
        finally:
            try:
                if not session.config.getoption("skip_data_collector"):
                    collect_created_resources(session_store=_session_store, data_collector_path=_data_collector_path)
            finally:
                try:
                    if session.config.getoption("skip_teardown"):
                        LOGGER.warning("User requested to skip teardown of resources")
                    else:
                        try:
                            session_teardown(session_store=_session_store)
                        except Exception:
                            LOGGER.exception("Resources remain after session teardown")
                            if not session.config.getoption("skip_data_collector"):
                                run_must_gather(data_collector_path=_data_collector_path)
                            raise
                finally:
                    shutil.rmtree(path=session.config.option.basetemp, ignore_errors=True)
    except Exception as exp:
        cleanup_error = exp

    # --- Inner session hooks: retain a raised exception so reporting still runs ---
    session_outcome: object = None
    try:
        session_outcome = yield
    except Exception as exp:
        inner_error = exp

    # --- Post-yield reporting: needs other plugins' session-finish output ---
    try:
        try:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            reporter.summary_stats()
        finally:
            if session.config.getoption("analyze_with_ai"):
                if exitstatus == 0:
                    LOGGER.info(f"No test failures (exit code {exitstatus}), skipping AI analysis")
                else:
                    try:
                        enrich_junit_xml(session)
                    except Exception:
                        LOGGER.exception("Failed to enrich JUnit XML, original preserved")
                        raise
    except Exception as post_yield_error:
        # A post-yield failure must not swallow earlier errors: chain it from the earliest
        # failure, linking the other earlier failure into the same chain so every traceback
        # stays visible.
        if cleanup_error is not None and inner_error is not None:
            inner_error.__cause__ = cleanup_error
            raise post_yield_error from inner_error
        earlier_error = cleanup_error or inner_error
        if earlier_error is not None:
            raise post_yield_error from earlier_error
        raise

    # --- Error propagation: the earliest failure wins, later ones chain from it ---
    if inner_error is not None:
        if cleanup_error is not None:
            raise inner_error from cleanup_error
        raise inner_error
    if cleanup_error is not None:
        raise cleanup_error
    return session_outcome


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
    #        - Create a skip marker with a descriptive reason
    #        - Iterate over items and add the marker where the keyword matches
    #   3. Apply the marker to the relevant test class, e.g.:
    #        @pytest.mark.my_new_marker
    #        class TestMyFeature: ...
    # -------------------------------------------------------------------
    # Resolve each item's plan config once: the skip loops and the later rename loop share
    # the same resolution (parametrization fields or item.originalname).
    resolved_plan_configs = [(item, resolve_item_plan_config(item)) for item in items]
    if not is_dry_run(config):
        providers_json_path = config.getoption("providers_json", default=None)
        providers = load_source_providers(providers_json_path=providers_json_path)
        # .get() with default is intentional: source_provider may not be configured (e.g., partial config),
        # in which case provider-type gating is silently skipped. In pool mode the provider key is
        # unresolved at collection time (the latency fixture runs later), so the pool's
        # type key drives the gating instead.
        source_provider_type = providers.get(py_config.get("source_provider", ""), {}).get("type") or py_config.get(
            "source_provider_type"
        )

        if source_provider_type:
            # Skip warm migration tests for providers that do not support warm migration.
            warm_unsupported = (
                Provider.ProviderType.OPENSTACK,
                Provider.ProviderType.OPENSHIFT,
                Provider.ProviderType.OVA,
                Provider.ProviderType.HYPERV,
            )
            if source_provider_type in warm_unsupported:
                warm_skip = pytest.mark.skip(reason=f"{source_provider_type} warm migration is not supported.")
                for item in items:
                    if "warm" in item.keywords:
                        item.add_marker(warm_skip)

            # Skip vSphere-marked tests unless they also declare the configured source
            # provider, and skip vSphere-only feature tests, on non-vSphere runs.
            if source_provider_type != Provider.ProviderType.VSPHERE:
                vsphere_only_skip = pytest.mark.skip(reason="Test is only applicable to vSphere source providers")
                configured_provider_marker = _PROVIDER_TEST_MARKERS[source_provider_type]
                for item in items:
                    is_vsphere_scoped = "vsphere" in item.keywords and configured_provider_marker not in item.keywords
                    if is_vsphere_scoped or any(
                        kw in item.keywords for kw in ("copyoffload", "shared_disk", "deep_inspection", "aap", "luks")
                    ):
                        item.add_marker(vsphere_only_skip)

            # Skip CA cert tests for providers that don't use CA certificates.
            ca_cert_unsupported = (
                Provider.ProviderType.OPENSHIFT,
                Provider.ProviderType.OVA,
            )
            if source_provider_type in ca_cert_unsupported:
                ca_cert_skip = pytest.mark.skip(
                    reason=f"{source_provider_type} does not use CA certificates in provider secrets"
                )
                for item in items:
                    if "ca_crt" in item.keywords:
                        item.add_marker(ca_cert_skip)

            # Skip tests whose plan config requests `add_nic` on a non-vSphere provider.
            # `add_nic` is a plan config flag, not a marker, so resolve each item's config.
            if not supports_add_nic(source_provider_type):
                add_nic_skip = pytest.mark.skip(
                    reason=f"add_nic is vSphere-only; skipping for provider '{source_provider_type}'"
                )
                for item, test_config in resolved_plan_configs:
                    if test_config is None:
                        continue
                    # `virtual_machines` is a required plan-config field: direct access fails the
                    # collection run with a clear KeyError instead of a later fixture error.
                    if any(vm.get("add_nic") for vm in test_config["virtual_machines"]):
                        item.add_marker(add_nic_skip)

    # In pool mode the provider is unresolved at collection time; the pool type labels the items.
    provider_label = py_config.get("source_provider") or py_config.get("source_provider_type")

    for item, _ in resolved_plan_configs:
        item.name = f"{item.name}-{provider_label}-{py_config.get('storage_class')}"


def pytest_collection_finish(session: pytest.Session) -> None:
    """Collect the base VM entries needed by the tests selected for this run.

    Runs after all deselection (``-k``, ``-m``, path selection, ``--ignore``), so only VMs
    of tests that will actually execute are collected. Items skipped at collection time
    (provider gating) never execute fixtures, so their VMs are not needed. Each entry keeps
    the ``clone`` flag so the existence snapshot can apply copy-offload name overrides.

    Args:
        session (pytest.Session): Pytest session holding the final selected items.
    """
    # Entry shape is {name: str, clone: bool}, built from free-form `tests_params` VM data whose
    # values are heterogeneous (hence Any, like all plan-config data).
    vms_for_current_session: list[dict[str, Any]] = []
    for item in session.items:
        if item.get_closest_marker("skip"):
            continue
        test_config = resolve_item_plan_config(item=item)
        if test_config and "virtual_machines" in test_config:
            for vm in test_config["virtual_machines"]:
                vms_for_current_session.append({"name": vm["name"], "clone": vm.get("clone", False)})

    get_fixture_store(session)["vms_for_current_session"] = vms_for_current_session
    # Logged in every mode, including `--collect-only`: the run command decides the needed VMs.
    LOGGER.info(
        f"Base VMS names for current session:\n {'\n'.join(sorted({vm['name'] for vm in vms_for_current_session}))}"
    )


def _handle_failure_must_gather(node: pytest.Item, call: pytest.CallInfo[BaseException]) -> None:
    """Collect or defer a must-gather for a failing test item.

    Standalone tests gather immediately, once per worker-qualified identity. Their setup/call
    collector errors propagate to the caller, which stashes them on the item for teardown.
    Class setup/call failures are marked pending for the class-plan boundary; when the remote
    debugger is enabled the owed gather runs immediately against the still-alive class Plan so
    diagnostics precede the blocking post-mortem session. Class teardown failures gather through
    the boundary-aware teardown path.

    Args:
        node (pytest.Item): The test item reporting the exception.
        call (pytest.CallInfo[BaseException]): The call info of the failing phase.

    Raises:
        Exception: Whatever the attempted collection raises, for the caller to defer or re-raise.

    Returns:
        None: The helper only records or collects diagnostics.
    """
    node_cls: type | None = getattr(node, "cls", None)
    if node_cls is None:
        collected: set[str] = getattr(node.session, "_must_gather_collected_tests", set())
        worker: str = getattr(node.config, "workerinput", {}).get("workerid", "master")
        identity = f"{node.nodeid}::worker={worker}"
        collector_error = node.stash.get(_STANDALONE_MUST_GATHER_ERROR, None)
        if call.when == "teardown" and collector_error is not None:
            del node.stash[_STANDALONE_MUST_GATHER_ERROR]
            return
        if identity not in collected and collect_must_gather_for_item(node, identity, plan_obj=None):
            collected.add(identity)
            setattr(node.session, "_must_gather_collected_tests", collected)
        return

    if call.when == "teardown":
        class_collector_error = node.stash.get(_CLASS_MUST_GATHER_ERROR, None)
        if class_collector_error is not None:
            # The teardown wrapper already reported this collector error; retrying the gather here
            # would raise inside the hook and abort the worker with an INTERNALERROR. The identity
            # stays pending for the session-end flush.
            del node.stash[_CLASS_MUST_GATHER_ERROR]
            return
        collect_class_teardown_must_gather(node)
        return

    mark_class_pending_must_gather(node)
    if remote_debugger_enabled():
        # The post-mortem debugger blocks the process until a client detaches, so the owed gather
        # runs now, against the still-alive class Plan, instead of waiting for the class-plan
        # boundary. Success marks the identity collected and the boundary skips it; a failed
        # attempt stays pending for the boundary retry and the session-end flush.
        collect_pending_class_must_gather_now(node)


def pytest_exception_interact(node, call, report):
    """Defer or collect a must-gather for a failing test, then open the remote post-mortem debugger.

    Setup/call failures record the failing item by class-plan identity. The tryfirst teardown hook
    binds Plan context at the nextitem boundary before default fixture finalization for all retries.
    With no plan, it binds None and takes a full gather. Standalone tests gather immediately, once on success.
    Collection-phase failures from collectors carry no test context and are left to pytest's own
    reporting; the hook returns without gathering so a collection error stays a collection error.
    Unexpected standalone setup/call collector errors are retained on the item until fixture finalization.
    Earlier method teardown failures are deferred while the same class-plan continues. Boundary
    finalizer failures arrive after cleanup and gather only if no successful gather already exists.

    Args:
        node (pytest.Item): The test item reporting the exception.
        call (pytest.CallInfo[BaseException]): The call info of the failing phase.
        report (pytest.TestReport): The report of the failing phase.

    Returns:
        None: The hook only records or collects diagnostics.
    """
    if is_dry_run(node.session.config):
        return

    if call.when == "collect":
        # Collection failures from collectors (a Module that fails to import, or a Class collector
        # whose collection failed) have no test context; pytest reports them itself. This check runs
        # before the class/standalone split because a pytest.Class collector reports `cls` as set.
        # Attempting a gather here would re-raise inside this hook and turn a reportable collection
        # error into an INTERNALERROR.
        return

    if not node.session.config.getoption("skip_data_collector"):
        try:
            _handle_failure_must_gather(node=node, call=call)
        except Exception as exp:
            if getattr(node, "cls", None) is None and call.when in ("setup", "call"):
                # Retain a standalone collector error on this item so setup/call reporting can
                # reach fixture teardown; the teardown wrapper re-raises it as the item's error.
                node.stash[_STANDALONE_MUST_GATHER_ERROR] = exp
            else:
                raise

    # Remote post-mortem debugger (MTV_REMOTE_PDB). Reached by every non-dry-run, non-collect
    # failure regardless of --skip-data-collector, after must-gather handling so diagnostics
    # precede the blocking debugger session: standalone gathers already ran, and class gathers
    # ran immediately above when the debugger is enabled (otherwise they are deferred to the
    # class-plan boundary). Skipped when --pdb is active so the local terminal debugger takes
    # precedence, and skipped for BdbQuit so quitting a code breakpoint fails that test without
    # opening another listener. A debugger quit ends the test session, matching pytest's native
    # --pdb behavior. usepdb has a default because -p no:pdb removes the option.
    if (
        remote_debugger_enabled()
        and not node.config.getoption("usepdb", default=False)
        and call.excinfo is not None
        and not isinstance(call.excinfo.value, bdb.BdbQuit)
    ):
        try:
            remote_post_mortem(exception=call.excinfo.value)
        except bdb.BdbQuit:
            pytest.exit("Quitting remote debugger")


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
    selected_source_provider,
    source_provider_data,
    nfs_storage_profile,
    storage_canary,
    base_resource_name,
    forklift_pods_state,
    virtctl_binary,
):
    # selected_source_provider runs first so pool-mode runs resolve their
    # provider before anything consumes it. source_provider_data is called here to
    # fail fast if the provider is not found in the providers list from config.
    # storage_canary (which creates target_namespace) runs before any class-scoped
    # fixture so broken destination storage fails the session before migrations run.
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
def storage_canary(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    session_uuid: str,
) -> None:
    """Fail fast when destination storage provisioning is permanently failing.

    Creates a small canary PVC in the target namespace using the run's configured
    storage class, before any migration runs. Healthy storage binds it in seconds;
    a canary that is still Pending after the bind timeout fails the session.
    WaitForFirstConsumer storage classes are skipped (a bare canary PVC cannot
    bind without a consumer).

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for the target cluster.
        target_namespace (str): Target namespace for the canary PVC.
        session_uuid (str): Session UUID, used for the canary PVC name.

    Raises:
        StorageProvisioningError: If the destination storage class cannot provision volumes.
        ValueError: If the configured storage class does not exist on the cluster.
    """
    verify_destination_storage_provisioning(
        client=ocp_admin_client,
        namespace=target_namespace,
        session_uuid=session_uuid,
    )


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
def target_ocp_version(ocp_admin_client: DynamicClient) -> str:
    """Target (destination) OpenShift cluster version as "major.minor".

    Derived from the cluster the run targets, so generated VM names always
    match the actual target cluster version.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for the target cluster.

    Returns:
        str: Cluster version formatted as "major.minor" (e.g. "4.22").

    Raises:
        ValueError: If the cluster version cannot be determined or parsed.
    """
    version = get_cluster_version(client=ocp_admin_client)
    return f"{version.major}.{version.minor}"


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
        name=FORKLIFT_CONTROLLER_NAME,
        namespace=mtv_namespace,
        ensure_exists=True,
    )

    snapshots_interval = int(py_config["snapshots_interval"])

    forklift_controller.wait_for_condition(
        status=forklift_controller.Condition.Status.TRUE,
        condition=forklift_controller.Condition.Type.RUNNING,
        timeout=300,
    )

    current_interval = getattr(forklift_controller.instance.spec, "controller_precopy_interval", None)

    if str(current_interval) == str(snapshots_interval):
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
    selected_source_provider: str,
    source_providers: dict[str, dict[str, Any]],
    fixture_store: dict[str, Any],
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    """Resolve source provider configuration from the loaded providers data.

    Args:
        selected_source_provider (str): The explicit or latency-selected provider
            key; requested directly so selection always runs before this fixture.
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

    requested_provider = selected_source_provider
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

    virtual_machines = [dict(vm) for vm in class_plan_config["virtual_machines"]]
    # Copy-offload configs use a placeholder VM name; the real name comes from .providers.json
    apply_copyoffload_vm_name_override(virtual_machines=virtual_machines, source_provider=source_provider)
    vms = [vm["name"] for vm in virtual_machines]
    LOGGER.info(f"Found VMs for network lookup: {vms}")

    # Query networks using provider abstraction (handles templates vs VMs internally)
    networks = source_provider.get_vm_or_template_networks(names=vms, inventory=source_provider_inventory)

    if not networks:
        raise ValueError(f"No networks found for VMs {vms}. VMs must have at least one network interface.")

    # Calculate how many multus NADs we need
    extra_nics = sum(1 for vm in virtual_machines if vm.get("add_nic"))
    if class_plan_config.get("per_nic_network_map", False):
        multus_count = max(
            0,
            len(
                get_per_nic_networks(
                    source_provider=source_provider, source_provider_inventory=source_provider_inventory, vms=vms
                )
            )
            - 1
            + extra_nics,
        )
    else:
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


@pytest.fixture(scope="session")
def source_vms_existence(
    fixture_store: dict[str, Any],
    selected_source_provider: str,
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
) -> dict[str, bool]:
    """Snapshot which run-needed VMs exist in the source provider inventory, once per session.

    The Forklift inventory is what MTV actually migrates, so it is the source of truth for
    VM existence. The snapshot is logged (found summary plus one ERROR line per missing VM),
    stored in the session store, and returned for reuse by test classes. The OpenShift source
    is skipped because its VMs are created at runtime. Dry runs never execute fixtures, so
    the provider is never queried for ``--collect-only``.

    Args:
        fixture_store (dict[str, Any]): Session fixture store.
        selected_source_provider (str): Configured name of the source provider.
        source_provider (BaseProvider): Source provider instance.
        source_provider_inventory (ForkliftInventory): Forklift inventory of the source provider.

    Returns:
        dict[str, bool]: VM name to existence in the provider inventory; empty when skipped.
    """
    if source_provider.type in (Provider.ProviderType.OPENSHIFT,):
        fixture_store["source_vms_existence"] = {}
        LOGGER.info(
            f"Source provider '{selected_source_provider}' (type {source_provider.type}) creates "
            "VMs at runtime; skipping VM existence snapshot"
        )
        return {}

    # Copy-offload overrides replace placeholder names after collection: apply the same
    # override so the snapshot checks the effective names the classes actually use.
    virtual_machines = [dict(vm) for vm in fixture_store["vms_for_current_session"]]
    apply_copyoffload_vm_name_override(virtual_machines=virtual_machines, source_provider=source_provider)
    vm_names = sorted({vm["name"] for vm in virtual_machines})

    # RHV clones from templates, which the Forklift inventory does not list (see
    # get_vm_or_template_networks): template existence is verified on the provider itself.
    check_scope = (
        f"on source provider '{selected_source_provider}' [{source_provider.host}] "
        f"(type {source_provider.type}; templates are not listed in the inventory)"
        if isinstance(source_provider, OvirtProvider)
        else f"in the inventory of source provider '{selected_source_provider}' "
        f"[{source_provider.host}] (type {source_provider.type})"
    )
    LOGGER.info(f"Checking existence of {len(vm_names)} needed VMs {check_scope}")
    missing = get_missing_source_vms(
        source_provider=source_provider,
        source_provider_inventory=source_provider_inventory,
        vm_names=vm_names,
    )
    vm_existence: dict[str, bool] = {name: name not in missing for name in vm_names}
    fixture_store["source_vms_existence"] = vm_existence

    LOGGER.info(
        f"Source VM existence {check_scope}: found: {sorted(name for name, exists in vm_existence.items() if exists)}"
    )
    for name in missing:
        LOGGER.error(f"VM '{name}' needed by the current run was not found {check_scope}")
    return vm_existence


@pytest.fixture(scope="class")
def validated_source_vms(
    class_plan_config: dict[str, Any],
    selected_source_provider: str,
    source_provider: BaseProvider,
    source_vms_existence: dict[str, bool],
) -> None:
    """Validate that the class's source VMs exist in the provider inventory before any test setup.

    Uses the session-level existence snapshot, which already covers copy-offload effective
    names. Runs once per test class as early as possible to fail fast before cloning,
    namespace creation, or other expensive setup. The OpenShift source is skipped because
    its VMs are created at runtime.

    Args:
        class_plan_config (dict[str, Any]): Plan configuration from parametrization.
        selected_source_provider (str): Configured name of the source provider.
        source_provider (BaseProvider): Source provider instance.
        source_vms_existence (dict[str, bool]): Session VM existence snapshot.

    Raises:
        VmNotFoundError: If one or more VMs are not found.
    """
    if source_provider.type in (Provider.ProviderType.OPENSHIFT,):
        return

    virtual_machines = [dict(vm) for vm in class_plan_config["virtual_machines"]]
    apply_copyoffload_vm_name_override(virtual_machines=virtual_machines, source_provider=source_provider)
    missing = [vm["name"] for vm in virtual_machines if not source_vms_existence.get(vm["name"])]
    raise_for_missing_source_vms(
        source_provider=source_provider,
        provider_name=selected_source_provider,
        missing=missing,
    )


@pytest.fixture(scope="class")
def prepared_plan(
    request: pytest.FixtureRequest,
    class_plan_config: dict[str, Any],
    validated_source_vms: None,
    fixture_store: dict[str, Any],
    source_provider: Any,
    source_vms_namespace: str,
    ocp_admin_client: DynamicClient,
    multus_cni_config: str,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
    vcenter_clone_provider: VMWareProvider | None,
    source_provider_data: dict[str, Any],
) -> Generator[dict[str, Any], None, None]:
    """Prepare plan with cloned VMs for class-based tests.

    This fixture handles VM cloning and name updates, similar to the
    function-scoped `plan` fixture but at class scope. It prepares VMs
    once per test class rather than once per test function.

    Cloning uses a two-phase pattern: all VMs are cloned first, then Forklift
    inventory sync is waited on for every cloned VM. This avoids inventory sync
    failures when cloning VM2+ while VM1 inventory sync is still pending.

    Args:
        request (pytest.FixtureRequest): Pytest fixture request
        class_plan_config (dict[str, Any]): Plan configuration from parametrization
        validated_source_vms (None): Ensures source VMs/templates exist before setup
        fixture_store (dict[str, Any]): Fixture store for resource tracking
        source_provider: Source provider instance (VMWareProvider, OvirtProvider, etc.)
        source_vms_namespace (str): Source VMs namespace
        ocp_admin_client (DynamicClient): OpenShift client
        multus_cni_config (str): Multus CNI configuration
        source_provider_inventory (ForkliftInventory): Source provider inventory
        target_namespace (str): Default target namespace for VMs
        vcenter_clone_provider (VMWareProvider | None): vCenter provider for cloning, or None
        source_provider_data (dict[str, Any]): Source provider configuration data

    Yields:
        dict[str, Any]: Prepared plan with updated VM names
    """

    # Deep copy the plan config to avoid mutation
    plan: dict[str, Any] = deepcopy(class_plan_config)

    if "pvc_name_template" in plan:
        plan["pvc_name_template"] = resolve_pvc_name_template(
            pvc_name_template=plan["pvc_name_template"],
            source_provider_type=source_provider.type,
        )

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
    apply_copyoffload_vm_name_override(virtual_machines=virtual_machines, source_provider=source_provider)

    if plan.get("disable_drs_for_vms", False) and source_provider.type == Provider.ProviderType.OVA:
        raise ValueError("disable_drs_for_vms is not supported for OVA providers")

    # Original power states of skip_clone VMs, restored in teardown below since these are
    # shared, real VMs (not disposable clones) that other tests may depend on.
    original_power_states: list[tuple[str, str]] = []

    if source_provider.type != Provider.ProviderType.OVA:
        openshift_source_provider: bool = source_provider.type == Provider.ProviderType.OPENSHIFT

        # Resolved lazily via request so OVA plans (which skip VM-name suffix generation)
        # never parse the cluster version.
        vm_name_suffix = get_vm_suffix(
            warm_migration=warm_migration,
            ocp_version=request.getfixturevalue("target_ocp_version"),
        )

        if openshift_source_provider:
            # OCPProvider.get_vm_by_name resolves VMs only within this namespace:
            # concurrent sessions create identically-named VMs in their own
            # source namespaces.
            source_provider.source_vms_namespace = source_vms_namespace

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

        # Track original VM names and cloned objects for shared disk relinking.
        # Check for `is not None` because the field can be True (owner) or False (consumer),
        # and both need relinking. We only skip VMs without this field at all.
        plan_level_shared_disks = bool(plan.get("migrate_shared_disks"))
        has_shared_disk_config = plan_level_shared_disks or any(
            vm.get("migrate_shared_disks") is not None for vm in virtual_machines
        )
        # Fail early before cloning if shared-disk relinking is requested on an unsupported provider.
        if has_shared_disk_config and not isinstance(source_provider, VMWareProvider):
            raise ValueError(
                f"Shared-disk migration requested, but provider '{source_provider.type}' "
                "does not implement relink_shared_disks"
            )

        # The non-vSphere provider case is gated at collection time in
        # pytest_collection_modifyitems (pytest.skip must not be used in fixtures).
        has_add_nic_config = any(vm.get("add_nic") for vm in virtual_machines)
        if has_add_nic_config:
            for vm in virtual_machines:
                if vm.get("add_nic"):
                    if "add_nic_start_connected" not in vm:
                        raise ValueError(
                            f"VM '{vm['name']}': add_nic=True requires add_nic_start_connected to be set explicitly"
                        )
                    if not isinstance(vm["add_nic_start_connected"], bool):
                        raise ValueError(
                            f"VM '{vm['name']}': add_nic_start_connected must be a bool, "
                            f"got {type(vm['add_nic_start_connected']).__name__!r}"
                        )

        if plan.get("disable_drs_for_vms", False) and not isinstance(clone_provider, VMWareProvider):
            raise ValueError(
                f"disable_drs_for_vms requires a VMware clone provider, got '{type(clone_provider).__name__}'"
            )

        if plan.get("preserve_static_ips", False):
            for vm in virtual_machines:
                if vm.get("source_vm_power") != "on":
                    raise ValueError(
                        f"preserve_static_ips requires source_vm_power='on' for VM '{vm['name']}'. "
                        "Guest tools must be running to collect static IP and NIC name data."
                    )

        original_source_vm_names: list[str] = [vm["name"] for vm in virtual_machines] if has_shared_disk_config else []
        cloned_vm_objects: list[Any] = []
        cloned_vm_names: list[str] = []
        first_vm_esxi_host: str | None = None
        non_dedicated_host_name: str | None = None
        # VM name -> expected total NIC count after add_nic; used to wait for a fresh inventory
        # that includes the added NIC before NetworkMap creation.
        added_nic_expected_counts: dict[str, int] = {}

        skip_clone = plan.get("skip_clone", False)

        if skip_clone:
            if not source_provider.supports_skip_clone():
                raise ValueError(
                    f"skip_clone=True is not supported for provider type '{source_provider.type}'; "
                    "VMs must be cloned from templates."
                )
            skip_clone_incompatible = ["disable_drs_for_vms", "clone_to_same_host"]
            if source_provider.type != Provider.ProviderType.HYPERV:
                skip_clone_incompatible.append("preserve_static_ips")
            conflicting = [flag for flag in skip_clone_incompatible if plan.get(flag)]
            if has_shared_disk_config:
                conflicting.append("migrate_shared_disks")
            if has_add_nic_config:
                conflicting.append("add_nic")
            if conflicting:
                raise ValueError(
                    f"skip_clone=True is incompatible with {conflicting}; these options require the cloning phase."
                )

        if skip_clone:
            for vm in virtual_machines:
                source_vm_power = vm.get("source_vm_power")
                if source_vm_power:
                    provider_vm_api = source_provider.get_vm_by_name(vm["name"])
                    original_power_states.append((
                        vm["name"],
                        "on" if source_provider.is_vm_powered_on(provider_vm_api) else "off",
                    ))
                    if source_vm_power == "on":
                        source_provider.start_vm(provider_vm_api)
                        if source_provider.type == Provider.ProviderType.VSPHERE:
                            source_provider.wait_for_vmware_guest_info(
                                provider_vm_api, timeout=class_plan_config.get("guest_agent_timeout", 120)
                            )
                        elif source_provider.type == Provider.ProviderType.HYPERV:
                            source_provider.wait_for_guest_network_config(
                                vm=provider_vm_api,
                                timeout=class_plan_config.get("guest_agent_timeout", 120),
                            )
                    elif source_vm_power == "off":
                        source_provider.stop_vm(provider_vm_api)

                if plan.get("preserve_static_ips", False):
                    source_vm_details = source_provider.vm_dict(
                        provider_vm_api=provider_vm_api,
                        name=vm["name"],
                        namespace=source_vms_namespace,
                        clone_options=vm,
                    )
                    plan["source_vms_data"][vm["name"]] = source_vm_details

        if not skip_clone:
            for vm in virtual_machines:
                clone_options = {**vm, "enable_ctk": warm_migration}

                # Pin VM2+ to same ESXi host as VM1 (required for per-host inflight throttling).
                # Uses setdefault to respect any explicit per-VM target_esxi_host override.
                if plan.get("clone_to_same_host", False) and first_vm_esxi_host:
                    clone_options.setdefault("target_esxi_host", first_vm_esxi_host)

                # Pin every VM to an ESXi host outside the configured dedicated hosts, so
                # dedicated-host verification can never coincide with a VM's own host (MTV-6136).
                if plan.get("pin_to_non_dedicated_host", False):
                    if non_dedicated_host_name is None:
                        non_dedicated_host_name = resolve_non_dedicated_esxi_host(
                            source_provider_inventory=source_provider_inventory,
                            source_provider_data=source_provider_data,
                        )
                    clone_options.setdefault("target_esxi_host", non_dedicated_host_name)

                provider_vm_api = clone_provider.get_vm_by_name(
                    query=vm["name"],
                    vm_name_suffix=vm_name_suffix,
                    clone_vm=True,
                    session_uuid=fixture_store["session_uuid"],
                    clone_options=clone_options,
                )

                # Capture first VM's ESXi hostname for subsequent same-host clones.
                if plan.get("clone_to_same_host", False) and first_vm_esxi_host is None:
                    runtime_host = provider_vm_api.runtime.host
                    if runtime_host is None or not runtime_host.name:
                        raise ValueError(
                            f"clone_to_same_host=True but could not determine ESXi host "
                            f"for VM '{vm['name']}'. Cannot pin subsequent clones."
                        )
                    first_vm_esxi_host = runtime_host.name
                    LOGGER.info(f"Same-host cloning: pinning subsequent VMs to ESXi host '{first_vm_esxi_host}'")

                # Disable DRS per VM to prevent relocation after cloning.
                if plan.get("disable_drs_for_vms", False) and isinstance(clone_provider, VMWareProvider):
                    clone_provider.disable_drs_for_vm(provider_vm_api)

                if has_shared_disk_config:
                    cloned_vm_objects.append(provider_vm_api)

                # Power state control: "on" = start VM, "off" = stop VM, not set = leave unchanged
                source_vm_power = vm.get("source_vm_power")  # Optional - if not set, VM power state unchanged
                if source_vm_power == "on":
                    source_provider.start_vm(provider_vm_api)
                    if source_provider.type == Provider.ProviderType.VSPHERE:
                        source_provider.wait_for_vmware_guest_info(
                            provider_vm_api, timeout=class_plan_config.get("guest_agent_timeout", 120)
                        )
                    elif source_provider.type == Provider.ProviderType.HYPERV:
                        source_provider.wait_for_guest_network_config(
                            vm=provider_vm_api,
                            timeout=class_plan_config.get("guest_agent_timeout", 120),
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
                    source=True,
                )
                vm["name"] = source_vm_details["name"]
                cloned_vm_names.append(vm["name"])

                provider_vm_api = source_vm_details["provider_vm_api"]

                vm["snapshots_before_migration"] = source_vm_details["snapshots_data"]
                # Store complete source VM data separately (keeps virtual_machines clean for Plan CR serialization)
                plan["source_vms_data"][vm["name"]] = source_vm_details

                # Clone the VM's secondary-network NIC onto a new NIC. This reconfigures the VM's
                # hardware AFTER it was already synced to the Forklift inventory during cloning, so the
                # inventory is now stale (missing the new NIC). A forced refresh + NIC-count wait runs
                # after the loop (see wait_for_added_nics_in_forklift_inventory) to guarantee NetworkMap
                # creation sees the added NIC — otherwise Forklift drops it during VM creation.
                if vm.get("add_nic"):
                    connected: bool = vm["add_nic_start_connected"]
                    nic_count_before = sum(
                        1
                        for dev in provider_vm_api.config.hardware.device
                        if isinstance(dev, vim.vm.device.VirtualEthernetCard)
                    )
                    mac = source_provider.add_nic(provider_vm_api, connected=connected)
                    if mac is not None:
                        vm["disconnected_nic_mac" if not connected else "connected_nic_mac"] = mac
                        added_nic_expected_counts[vm["name"]] = nic_count_before + 1

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
                    except (GuestCommandError, vim.fault.VimFault, TimeoutExpiredError, OSError) as e:
                        if plan.get("preserve_static_ips", False):
                            raise ValueError(
                                f"Failed to detect IP origins via Guest Operations for VM {vm['name']}: {e}. "
                                "IP origin detection is required when preserve_static_ips is enabled."
                            ) from e
                        LOGGER.warning(
                            f"Failed to detect IP origins via Guest Operations for VM {vm['name']}: {e}. "
                            "Static IP verification may not work for this VM."
                        )

                    if plan.get("preserve_static_ips", False):
                        detect_guest_nic_names(
                            source_provider=source_provider,
                            vm=provider_vm_api,
                            source_provider_data=fixture_store["source_provider_data"],
                            vm_details=source_vm_details,
                        )

            # Phase 2: wait for all cloned VMs in Forklift inventory after every clone completes.
            # Sequential per-VM wait during cloning causes inventory sync failures on VM2+.
            inventory_timeout = plan.get("inventory_timeout", 300)
            wait_for_cloned_vms_in_forklift_inventory(
                source_provider=source_provider,
                source_provider_inventory=source_provider_inventory,
                cloned_vm_names=cloned_vm_names,
                inventory_timeout=inventory_timeout,
            )

            # After add_nic, the cloned VM already existed in inventory so wait_for_cloned_vms
            # returns without seeing the new NIC. Force a refresh and block until each VM's
            # NIC count reflects the added NIC, so NetworkMap creation (later in the test)
            # maps every NIC and Forklift keeps it.
            if added_nic_expected_counts:
                wait_for_added_nics_in_forklift_inventory(
                    source_provider=source_provider,
                    source_provider_inventory=source_provider_inventory,
                    expected_nic_counts=added_nic_expected_counts,
                    timeout=inventory_timeout,
                )

            # Relink shared disks between clones (VMware-specific)
            # When VMs with shared disks are cloned, each clone gets independent disk copies,
            # breaking the shared disk relationship. This restores it on the clones.
            if has_shared_disk_config:
                clone_provider.relink_shared_disks(
                    source_vm_names=original_source_vm_names,
                    cloned_vms=cloned_vm_objects,
                )

        if plan.get("preserve_static_ips", False) and source_provider.type == Provider.ProviderType.HYPERV:
            wait_for_inventory_guest_networks(
                source_provider_inventory=source_provider_inventory,
                vm_names=[vm["name"] for vm in virtual_machines],
                source_vms_data=plan["source_vms_data"],
            )
    else:
        # OVA VMs aren't cloned — add unique targetName to prevent destination VM name conflicts
        # across parallel test sessions. The source VM name stays unchanged (must match OVA file).
        session_uuid = fixture_store["session_uuid"]
        for vm in virtual_machines:
            vm["targetName"] = sanitize_kubernetes_name(f"{session_uuid}-{vm['name']}")

    # Create Hooks if configured
    create_hook_if_configured(plan, "pre_hook", "pre", fixture_store, ocp_admin_client, target_namespace)
    create_hook_if_configured(plan, "post_hook", "post", fixture_store, ocp_admin_client, target_namespace)

    yield plan

    # Restore original power states of skip_clone VMs. Unlike cloned VMs (disposable), these
    # are real, shared VMs that other tests may depend on.
    for vm_name, original_power in original_power_states:
        provider_vm_api = source_provider.get_vm_by_name(vm_name)
        if original_power == "on":
            source_provider.start_vm(provider_vm_api)
        else:
            source_provider.stop_vm(provider_vm_api)

    # Reattach orphaned VMDKs to consumer clones so normal VM deletion cleans them up.
    # During relink, consumer clones' shared disks are redirected to the owner's VMDK,
    # leaving the consumer's original VMDK orphaned. Reattaching it here means
    # teardown's delete_vm will automatically remove it — no separate VMDK cleanup needed.
    vmdks_to_reattach: list[dict[str, Any]] = fixture_store["teardown"].pop("orphaned_vmdks_to_reattach", [])
    if vmdks_to_reattach:
        reattach_provider = vcenter_clone_provider or source_provider
        for vmdk_info in vmdks_to_reattach:
            try:
                reattach_provider.reattach_orphaned_vmdk(
                    vm_name=vmdk_info["vm_name"],
                    vmdk_path=vmdk_info["vmdk_path"],
                    bus_number=vmdk_info["bus_number"],
                    unit_number=vmdk_info["unit_number"],
                )
            except Exception:
                LOGGER.warning(
                    f"Failed to reattach VMDK '{vmdk_info['vmdk_path']}' to '{vmdk_info['vm_name']}', "
                    "VMDK may need manual cleanup"
                )

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
        vm_name = resolve_destination_vm_name(vm)
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
    return create_forklift_inventory(client=ocp_admin_client, mtv_namespace=mtv_namespace, provider=source_provider)


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
