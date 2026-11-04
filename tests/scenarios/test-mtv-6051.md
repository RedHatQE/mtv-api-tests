# Test Plan: MTV-6051 — SCSI Persistent Reservation on Shared RDM Disk Migration

## Objective

Verify that when migrating VMs with shared RDM disks from vSphere using
`rdmAsLun=true` and `migrateSharedDisks=true`, Forklift automatically sets
`lun.reservation: true` and `errorPolicy: report` on the target KubeVirt VM
disk spec. This enables SCSI-3 Persistent Reservations (pr-helper) for
clustered workloads and correct I/O error reporting for guest-level failover.

Feature PR: https://github.com/kubev2v/forklift/pull/7351
Epic: MTV-6049
Dev story: MTV-6050
QE story: MTV-6051
Feature request: MTV-5743

## Prerequisites

- `oc` with `mtv` plugin installed
- MTV installed on the cluster (with the PR #7351 fix)
- VDDK image configured
- Two vSphere VMs sharing an RDM disk (multi-writer mode)
- Environment variables set: `GOVC_URL`, `GOVC_USERNAME`, `GOVC_PASSWORD`

## Test Steps

1. Create namespace and vSphere + OpenShift providers
2. Create a migration plan with `--rdm-as-lun --migrate-shared-disks`
   for both VMs (owner + consumer)
3. Start the migration and wait for completion
4. For each migrated VM, inspect the KubeVirt VM spec:
   - Shared RDM disk has `lun.reservation: true`
   - Shared RDM disk has `errorPolicy: report`
   - Shared RDM disk has `shareable: true`
   - Shared RDM disk has `lun.bus: scsi`
5. Verify non-shared disks do NOT have reservation or errorPolicy set

## Pass Criteria

- Both VMs migrate successfully (plan status Succeeded)
- Shared RDM LUN disk on each VM has:
  - `devices.disks[N].lun.reservation == true`
  - `devices.disks[N].errorPolicy == "report"`
  - `devices.disks[N].shareable == true`
  - `devices.disks[N].lun.bus == "scsi"`
- Boot disk (non-RDM, non-shared) does NOT have `errorPolicy` or `reservation`

## Fail Criteria

- Migration fails
- `lun.reservation` is missing or false on the shared RDM disk
- `errorPolicy` is missing or not "report" on the shared RDM disk
- `reservation` or `errorPolicy` is set on non-shared disks (false positive)

## Cleanup

- Namespace deleted (includes all plans, VMs, PVCs, providers)
