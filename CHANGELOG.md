# Changelog

All notable changes to this plugin are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.2] - 2026-06-07

### Fixed
- **install.sh no longer fails on encrypted PegaProx DBs.** Newer PegaProx
  encrypts its SQLite DB (dbcrypto/SQLCipher), so an external `sqlite3` enable
  step failed with *"file is not a database (26)"* and — under `set -e` —
  aborted before restarting. The installer now probes for a plain DB, only
  writes `plugin_state` when it can, never aborts, always restarts, and clearly
  directs the operator to enable the plugin from **Settings → Plugins** in the
  UI (the encryption-agnostic path). README updated accordingly.

## [1.0.1] - 2026-06-07

### Fixed (self-review hardening, pre-deploy)
- Storage gate no longer waits on `unused<N>` (detached) disks — Proxmox does
  not need a detached volume's storage active to boot the guest, so gating start
  on it was wrongly over-strict.
- Execution now does a **live status re-check** immediately before each step:
  the plan is built moments earlier, so a guest may have changed state (a
  dependency's start, a manual action). Start/stop steps are now idempotent and
  race-safe instead of acting on stale plan state.
- `config/save` validates each member has an integer `vmid` → returns 400
  instead of a 500 on malformed input.
- `job` endpoint snapshots the job under the lock before serializing, avoiding a
  torn read while the executor thread mutates steps/log.
- Cluster selector shows the friendly cluster name (`manager.config.name`)
  instead of the internal id.

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
