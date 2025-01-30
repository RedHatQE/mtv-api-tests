import json
from pathlib import Path
from typing import Any

from ocp_resources.migration import Migration
from ocp_resources.plan import Plan
from ocp_resources.resource import ResourceEditor
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError

LOGGER = get_logger(__name__)


def session_teardown(session_store: dict[str, Any]) -> None:
    LOGGER.info("Running teardown to delete all created resources")
    session_teardown_resources = session_store["teardown"]

    try:
        cancel_migrations(migrations=session_teardown_resources.get("Migration", []))
        archive_plans(plans=session_teardown_resources.get("Plan", []))

    finally:
        for _resource_list in session_teardown_resources.values():
            for _resource in _resource_list:
                try:
                    _resource.clean_up(wait=True)
                except Exception as ex:
                    LOGGER.error(f"Failed to clean up {_resource.name} due to: {ex}")


def collect_created_resources(session_store: dict[str, Any], data_collector_path: Path) -> None:
    _created_reousrces: dict[str, list[dict[str, str]]] = {}

    for _resource_kind, _resource_list in session_store["teardown"].items():
        _created_reousrces.setdefault(_resource_kind, [])
        for _resource in _resource_list:
            LOGGER.info(f"Collecting data for resource {_resource.name}")
            try:
                _created_reousrces[_resource_kind].append({
                    "module": _resource.__module__,
                    "name": _resource.name,
                    "namespace": _resource.namespace,
                })

            except Exception as ex:
                LOGGER.error(f"Failed to collect data for resource {_resource.name} data due to: {ex}")

    if _created_reousrces:
        try:
            LOGGER.info(f"Write created resources data to {data_collector_path}/resources.json")
            with open(data_collector_path / "resources.json", "w") as fd:
                json.dump(_created_reousrces, fd)

        except Exception as ex:
            LOGGER.error(f"Failed to store resources.json due to: {ex}")


def cancel_migrations(migrations: list[Migration]) -> None:
    for migration in migrations:
        need_cancel = True
        migration_instance = migration.instance

        for condition in migration_instance.status.conditions:
            # No need to cancel migration if it's already succeeded
            if condition.type == "Succeeded" and condition.status == migration.Condition.Status.TRUE:
                need_cancel = False
                break

        if need_cancel:
            LOGGER.info(f"Canceling migration {migration.name}")
            migration_spec = migration.instance.spec
            plan = Plan(client=migration.client, name=migration_spec.plan.name, namespace=migration_spec.plan.namespace)
            ResourceEditor(
                patches={
                    migration: {
                        "spec": {
                            "cancel": plan.instance.spec.vms,
                        }
                    }
                }
            ).update()

            try:
                plan.wait_for_condition(condition="Canceled", status=plan.Condition.Status.TRUE)
            except Exception:
                LOGGER.error(f"Failed to cancel migration {migration.name}")


def archive_plans(plans: list[Plan]) -> None:
    for plan in plans:
        LOGGER.info(f"Archiving plan {plan.name}")

        ResourceEditor(
            patches={
                plan: {
                    "spec": {
                        "archived": True,
                    }
                }
            }
        ).update()

        try:
            plan.wait_for_condition(condition="Archived", status=plan.Condition.Status.TRUE)
        except TimeoutExpiredError:
            LOGGER.error(f"Failed to archive plan {plan.name}")
