<!-- Used by rootcoz when failure-history analysis is enabled.
     Complements .rootcoz/ROOTCOZ_PROMPT.md — answer this BEFORE classifying. -->

# Pre-Classification Check: Did an Earlier Test Leave State Behind?

## MANDATORY: answer this question first

**Did an earlier test in this run fail during setup or teardown and leave state that
explains this failure?**

If yes, this failure is a side effect, not an independent defect. The suite is
class-scoped and incremental: one broken class cascades into everything after it.

## How to check

1. **Scan `pytest-tests.log` for earlier teardown or setup failures**, before the
   failure under analysis. Look for:
   - `TimeoutExpiredError` during teardown, fixture finalization, or
     `cleanup_migrated_vms`
   - `SessionTeardownError`, or ERROR entries from `fixture_store` teardown while
     deleting a namespace, Plan, or VM
   - a class whose `test_check_vms` never ran because an earlier step failed
2. **Identify what the failed teardown was supposed to revert.** This suite changes
   more than the target namespace:
   - **Target cluster**: namespaces, Plan/Migration/StorageMap/NetworkMap/Provider CRs,
     VMs, PVCs, DataVolumes, and migration pods left behind by a failed cleanup
   - **Source vSphere**: per-VM DRS is disabled (`disable_drs_for_vms`) and VMs may be
     pinned to a specific ESXi host; a teardown that never ran leaves DRS off and VMs
     pinned, which changes scheduling for every later migration
   - **Copy-offload**: dedicated migration hosts and populator pods; a stuck populator
     holds storage paths and makes the next XCOPY run fail
   - **Guest-level changes**: hooks that run inside the guest, extra disks added by
     `add_disks`, and cloned VMs that still exist on the source
   - **Lifecycle tests**: `tests/plan_lifecycle/` archives and deletes Plans on purpose,
     and resume-conversion tests deliberately leave PVCs in place — their leftovers are
     expected and must not be blamed on the next test
3. **Check whether the current symptom matches that un-reverted state.** Examples:
   migration stuck in `CopyingPods` because a leftover populator pod holds the volume;
   `Plan` stuck `NotReady` because the source VM is already locked by a stale migration;
   PVCs `Pending` because the archived plan's PVCs were never released; a migration
   placed on the wrong host because DRS is still disabled.

## Classification

- **The test whose setup/teardown failed**: classify on why the cleanup failed —
  `CODE ISSUE` if the cleanup logic is wrong, `PRODUCT BUG` if the product refused to
  release or finalize a resource, `INFRASTRUCTURE` if the environment blocked cleanup
  (storage backend, API outage, source provider unreachable).
- **Every later failure in the same run caused by it**: use the **same classification**
  as the root-cause test, and state it in `details` as
  `Caused by <test_name> teardown failure — <resource> was not reverted`.
- **Mark them as derivative, not as separate defects.** rootcoz counts defects; a
  cascade inflating the defect count is worse than one honest root cause.

## When this check does NOT apply

- This is the first failure in the run
- No setup/teardown error appears before it in `pytest-tests.log`
- The failure is a collection error (`ImportError`, `SyntaxError`, missing fixture) —
  collection happens before any test runs
- The cluster or source provider was already broken before the first test (deployment
  failure, MTV operator not installed, provider unreachable from the start)
- The failure is in a different area than what the earlier test touched (storage
  leftover vs. a CPU-topology assertion, for example)
- Failure history shows the same error recurring on runs with no preceding teardown
  failure — that points to a recurring defect, not a cascade

In all of these cases, continue with the normal classification rules.
