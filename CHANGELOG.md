# Changelog

All notable changes to this plugin are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-06-07

### Added
- Initial release of **Proxmox VM Power Control**.
- Cross-node dependency graph (`depends_on` + `order`/`suborder`) with
  topological ordering for start and reverse ordering for stop; cycle and
  unknown-dependency detection.
- Pre-flight checklist: cluster/host availability, node status, storage
  availability + local/remote classification (NFS/iSCSI/CIFS/NVMe-oF/…),
  VM/CT boot settings, master-vs-standalone posture.
- Execution engine with storage gate (loop), health-gating per member
  (`agent` / `status` / `delay`), graceful `shutdown` vs hard `stop`, and a
  background job model with live progress.
- **Dry-run by default**; real power actions require explicit `confirm:true`.
- REST API (`clusters`, `inventory`, `config`, `config/save`, `preflight`,
  `plan`, `execute`, `job`, `jobs`) reusing PegaProx's authenticated manager.
- Embedded dashboard (`ui`): cluster selector, group list, pre-flight, plan
  preview, live job progress, inventory, jobs, JSON config editor.
- RBAC (`vm.view` / `vm.power`) and audit logging on mutations.
- 21 unit/engine tests runnable without PegaProx or a live Proxmox.
