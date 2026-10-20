# Test class docstring plan schema

Every new pytest test class MUST put its complete product test plan in its **class docstring**.
A customer must be able to understand and repeat the scenario manually without reading Python,
repository configuration, or automation instructions. Describe the product, not the test runner.

The structural part of this schema is enforced by the `check-test-plan-docstring-schema` pre-commit
hook: every test class must have a docstring, and it must contain the sections below, in order, with
a summary line and consecutively numbered steps. There is no baseline and no suppression - the hook
always reflects the current state of the files. The content-quality checklist below remains
human-review scope.

## Required content

Use these four sections in this order:

1. **Purpose/Regression:** State the product behavior or regression, why the scenario matters,
   and how the failure is induced. Distinguish a different failure path from the original bug when relevant.
2. **Prerequisites:** State how to register and connect the source and destination providers,
   prepare accessible destination storage and network, and verify required cluster capabilities.
   Specify source VM characteristics (guest, disks, network, power state), permissions, product features,
   and isolation. Describe equivalent resources a customer can supply; repository-specific VM names
   and image sources may be examples, never the only way to reproduce the test. Do not migrate a shared VM
   when the scenario deletes or alters it.
3. **Test plan:** Number actions and checks in execution order. Include manual preparation of all resources
   (including those automation creates), source inventory readiness, maps, hooks, Plan settings and Ready
   condition when applicable, migration/feature action, expected status or failed step, proof that the
   condition under test exists *before* cleanup, and deletion of every created resource (including source
   network attachments). State timeouts if they affect an observable check. Write each step as `N. text`
   at one indentation level; wrap long steps as deeper-indented continuation lines. Standalone unnumbered
   actions are not allowed — a non-step line must be a continuation of the numbered step above it.
4. **Expected result:** State product-visible pass criteria and where to inspect failure: resource
   conditions/status, VM migration step, relevant events/logs, and names of leftover resources.
   Do not substitute a pytest exception for a product outcome or claim assertions that the test does not make.

```python
class TestFeature:
    """[Product scenario in one sentence.]

    Purpose/Regression:
        [Behavior, regression, and chosen failure path.]

    Prerequisites:
        [Provider capabilities, disposable VM characteristics, network/storage,
        permissions, feature settings, and isolated environment.]

    Test plan:
        1. [Prepare resources and confirm source inventory visibility.]
        2. [Create mappings, hooks, and Plan; confirm readiness.]
        3. [Perform the action and observe product status or failed step.]
        4. [Confirm resources or effects exist before testing cleanup.]
        5. [Perform cleanup action and verify its status.]
        6. [Check final state and remove remaining test resources.]

    Expected result:
        [Observable pass criteria and product evidence to inspect on failure.]
    """
```

## Review checklist

Before review, verify every item:

- A customer can follow the plan using an equivalent disposable VM and registered providers with accessible
  destination storage and network, without this repository's VM names, CLI, fixtures, or configuration files.
- Provider-specific prerequisites and setup are explained as manual product steps. Do not mention automation
  defects, fixture behavior, or flags such as `--skip-teardown`. Product regression IDs may identify the behavior.
- Every numbered step says what to do **and what to observe**, in the same order as the test. Include Plan
  readiness and pre-cleanup evidence so a cleanup test cannot pass without exercising its failure condition.
- Final checks and manual cleanup account for all created resources, including provider-specific networks
  and retained VMs. Diagnostics use product-visible states and resource names rather than Python exceptions.
- All statements match the test's real assertions. Replace template steps for plan-readiness, expected-failure,
  or other scenarios rather than inventing migration or cleanup checks.
- Verify claims against the class's actual configuration: every check the plan describes must be driven by the
  class's real `tests_params` entries (VM options, plan flags such as `rdm_as_lun` or `xfs_compatibility`).
  Do not describe behavior that a missing config key disables.
- Qualify provider-conditional checks by provider: `check_vms()` asserts static IP preservation only for
  vSphere and Hyper-V sources and NIC names only for vSphere — plans must not claim these checks for other
  providers.
- Distinguish directly asserted outcomes from inferred ones: a migration reporting `Succeeded` does not prove
  a hook ran or a disk was written — name the evidence the test actually inspects.
