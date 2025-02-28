import os

import pytest
from kubernetes.dynamic.client import DynamicClient
from ocp_resources.console_config_openshift_io import Console
from playwright.sync_api import Browser, expect


@pytest.fixture(scope="session")
def cluster_data(ocp_admin_client: DynamicClient) -> tuple[str, str, str]:
    console_url = Console(client=ocp_admin_client, name="cluster").instance.status.consoleURL
    username = os.environ.get("CLUSTER_USERNAME")
    password = os.environ.get("CLUSTER_PASSWORD")
    if not username or not password:
        raise ValueError("CLUSTER_USERNAME and CLUSTER_PASSWORD must be set as environment variables")

    return username, password, console_url


@pytest.mark.ui
def test_basic_elements(request: pytest.FixtureRequest, cluster_data: tuple[str, str, str], browser: Browser) -> None:
    username, password, console_url = cluster_data
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    try:
        page.goto(console_url)
        page.get_by_label("Username").fill(username)
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Log in").click()
        page.wait_for_load_state()

        expect(page).to_have_title("Red Hat OpenShift")

        # Check the Migration side tab is visible
        expect(page.get_by_test_id("migration-nav-item")).to_be_visible(timeout=20_000)

        # Click on Migration tab
        page.get_by_test_id("migration-nav-item").click()

        # Check that Migration all sub tabs is visible
        # expect(page.get_by_role("link", name="Overview")).to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("providers-nav-item")).to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("plans-nav-item")).to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("network-mappings-nav-item")).to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("network-mappings-nav-item")).to_be_visible(timeout=10_000)

    except Exception:
        if not request.node.config.getoption("skip_data_collector"):
            page.screenshot(
                path=f"{request.node.config.getoption('data_collector_path')}/{request.node.name}/screenshot.png",
                full_page=True,
            )
        raise
