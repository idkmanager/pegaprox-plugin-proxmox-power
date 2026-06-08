# Changelog

All notable changes to this plugin are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.4.2] - 2026-06-07

### Added — make dependencies foolproof (Carlos kept getting an empty DEPENDE)
- **Quick dependency shortcuts** in the group editor: *"Todos dependen del 1º"*
  (hub — every other member depends on the first), *"Encadenar en orden"*
  (chain — each depends on the previous by order), and *"Quitar deps"*.
- A visible hint shows that the *"Arranca después de:"* chips are clickable
  (`+ off` → `✓ on`).
- Confirmed (live, 5-member group, deps set via chips): the plan's DEPENDE column
  renders `↳ <vmid>` correctly. The recurring empty column was groups saved
  without activating any dependency — now a single click sets them.

## [1.4.1] - 2026-06-07

### Fixed (group form, reported by Carlos)
- **No negative order/suborder.** Inputs now have `min` and values are clamped
  (order ≥ 1, suborder ≥ 0) on the client; the backend rejects order < 1 /
  suborder < 0.
- **No ambiguous duplicate order.** The (order, suborder) pair must be unique
  per group — the same `order` is allowed only with a different `suborder`.
  Enforced client-side (clear message) and in `config/save` (400). Duplicate
  vmids in a group are also rejected.
- **`update/apply` surfaces the real error** (e.g. "refusing downgrade …",
  "downloaded power.html is empty") instead of a generic "update failed".
- Verified the dependency round-trip end-to-end (create → save → plan shows
  `↳ dep`; reopen → chip restored active). The earlier "deps not shown" was a
  group saved without activating the chips, not a bug.

## [1.4.0] - 2026-06-07

### Changed / Hardened (closes the remaining QA-audit P3 findings)
- **Stop now verifies the node is reachable** (`_wait_node_online`, spec 1)
  before issuing shutdown/stop; maintenance does NOT block a stop (powering down
  during maintenance is legitimate). A stop on an offline node now fails with a
  clear message instead of a generic power error.
- **`apply_update` refuses a downgrade** (remote version strictly older than the
  installed one) unless `allow_downgrade: true` is passed; re-applying the same
  version is still allowed for repair. The UI/endpoint expose the flag.
- Test fixture `FakeManager._idx` moved to a per-instance attribute
  (parallel-test safe).
- Tests 41 → 43 (downgrade refusal + forced; stop-on-offline-node).

## [1.3.5] - 2026-06-07

### Hardened (independent QA audit follow-up)
- `_start_guest` / `_stop_guest` now guard against a member whose inventory
  record has no `node` (Proxmox omitted the field): the step fails with a clear
  "vmid X has no node in inventory" instead of building a malformed URL.
- `serve_ui` now also requires `vm.view` (defense in depth) on top of the
  catch-all's `plugins.view`.
- Tests 40 → 41 (node-guard). Independent audit verdict went CONDITIONAL→GO
  after these two P2 fixes; live E2E 13/13 green.

## [1.3.4] - 2026-06-07

### Improved
- **Dependencies are now obvious in the group form** (reported by Carlos: deps
  weren't being set, so the plan's DEPENDE column was empty). Each member row got
  a dedicated full-width *"Arranca después de:"* line with clear toggle chips
  (✓ on / + off) showing the other members by vmid + name — instead of a cramped
  column that was easy to miss. (Backend dependency handling was already correct;
  this is purely discoverability.)
- **The result frame is cleared when switching operation** (reported by Carlos:
  Pre-flight, Plan and job output used to stack). Pre-flight clears plan + job;
  Plan clears pre-flight + job; Execute clears pre-flight (keeps the plan and
  shows the job); selecting another group clears everything.

## [1.3.3] - 2026-06-07

### Changed
- Renamed the display name to **Powvm Control** (manifest, UI header/title,
  README, installer messages, systemd unit descriptions). The internal
  `plugin_id` (`proxmox-power`), API routes, install path, cache dir and repo
  are unchanged, so existing installs keep working without migration.

## [1.3.2] - 2026-06-07

### Fixed
- **Pre-flight now lists every node in the cluster**, not only the nodes hosting
  the group's members (reported by Carlos: a 1-member group showed a single
  host). Member-hosting nodes are marked `grupo` and are the only ones that gate
  the result; the rest are shown for full-cluster visibility and never turn the
  pre-flight red on their own.

## [1.3.1] - 2026-06-07

### Improved — robust local/remote deduction
- Local-vs-remote classification now falls back to the **cluster-level storage
  definition** (`/storage`, via `fetch_cluster_storage_defs`) when a storage has
  no live per-node status yet — so a guest's storage is still correctly deduced
  as local/remote even if it isn't active/listed on the node at that moment.
  Availability stays strictly per-node (only the live node status proves a
  storage is active); pre-flight now shows `no status on node` instead of a
  misleading INACTIVE when only the cluster def is known.
- `build_plan` gains an optional `storage_defs` argument; `plan`/`execute`/
  `preflight` pass the cluster defs through. Tests 39 → 40.

## [1.3.0] - 2026-06-07

### Added — visual config builder + clearer storage validation (UX)
- **Form-based group builder.** The Configuración tab no longer requires hand-
  written JSON: add a group, pick members from the **real cluster inventory**
  (dropdown), set order/suborder, toggle dependencies as chips, and choose
  health mode + storage_policy — all without JSON. Advanced settings collapse
  behind *Opciones avanzadas*; the raw JSON editor stays available under
  *Editar JSON (avanzado)* for power users (progressive disclosure).
  (Removes the missing-comma / misplaced-field class of errors entirely.)
- **Storage validation is now explicit in pre-flight.** A dedicated
  *Almacenamiento (NFS / iSCSI / CIFS / NVMe-oF / local)* section shows each
  guest's backing storage with a human **type label** (`storage_type_label`),
  local/remote placement and active/inactive state. Pre-flight checks now carry
  a `category` (cluster / node / storage / boot) and storage checks include
  `stype` / `placement`, so the UI groups them into readable sections.

### Tests
- 38 → 39 (`storage_type_label` mapping incl. NVMe-oF = shared LVM).

## [1.2.0] - 2026-06-07

### Added — auto-update + persistence
- **In-plugin auto-update.** New `update/check` and `update/apply` endpoints +
  an Actualizaciones panel in the UI. `apply` downloads the runtime files from a
  configurable `source` (default: this repo's raw GitHub), **validates them
  fail-closed** (manifest parses, `__init__.py` byte-compiles, `power.html`
  non-empty), backs up the old files and installs atomically, then the UI
  triggers PegaProx's `/reload` for a **live update with no service restart**.
  Configurable via the `updates` block in `config.json` (`source`,
  `auto_apply`, `check_interval_hours`).
- **Persistence across PegaProx upgrades.** `install.sh` now caches the plugin
  in `/usr/local/lib/proxmox-power` (outside `$PEGAPROX_DIR`) and installs a
  `proxmox-power-maintenance` systemd timer that, every 5 min, restores the
  plugin if a PegaProx upgrade wiped/downgraded it (re-copy + re-enable +
  restart) and — when `AUTO_UPDATE=true` — refreshes the cache from `source`.
  `uninstall.sh` removes the timer, cache and config.
- `version_tuple` / `version_gt` helpers (lenient semver compare).

### Tests
- 30 → 38 (version compare, check available/none/error, apply validates +
  rejects broken python / empty html, route wiring includes update endpoints).

## [1.1.0] - 2026-06-07

### Added (spec-coverage audit — close gaps vs the operator runbook)
- **Node availability loop (spec 1).** Before starting a guest the engine waits
  up to `host_wait_sec` for its node to be `online`, instead of failing on the
  power call.
- **HA maintenance check (spec 1.1).** New `fetch_ha_node_states` reads
  `/cluster/ha/status/manager_status`. Pre-flight reports `maint:<node>`, and a
  node in HA maintenance blocks start unless `ignore_maintenance` is set.
- **Explicit local vs remote branch (spec 8.1/8.2/9.1/9.2).** Start/stop now run
  through `_start_guest`/`_stop_guest` with a placement-aware branch, surfaced in
  step detail (`running + healthy [remote]`, `stopped [local]`, …).
- **Per-member `storage_policy`** (`wait` | `fail` | `skip`) — choose whether an
  inactive backing storage waits, fails the step, or skips the guest.
- **Per-member `health.timeout_sec`** is now honored (falls back to
  `step_timeout_sec`).
- New group settings `host_wait_sec` and `ignore_maintenance`.

### Changed
- `config.example.json` and README document every group/member option; all are
  editable from the Configuración tab.
- Test suite grown to 30 (added maintenance-blocks-start, ignore-maintenance,
  node-offline, storage_policy skip/fail, storage_policy plan defaults).

## [1.0.3] - 2026-06-07

### Fixed (found during live E2E on a real PegaProx)
- **Frontend now passes PegaProx CSRF.** State-changing `/api/*` calls require
  `X-Requested-With: XMLHttpRequest` or a matching Origin; the `api()` fetch
  wrapper now always sends the header (+ `credentials: same-origin`), so
  config-save / preflight / plan / execute work instead of returning 403.
- **install.sh sets the correct owner.** The plugin dir/config must be owned by
  the user the *pegaprox service* runs as (e.g. `pegaprox`), not the owner of
  `$PEGAPROX_DIR` (often `root`). Wrong ownership made the service unable to
  read/write `config.json` (Errno 13 → 500). Installer now derives the owner
  from `systemctl show -p User`, with sensible fallbacks.

### Verified live
- Full authenticated E2E against the production IDKMANAGER cluster: login →
  clusters (friendly names) → inventory (26 guests) → config/save → preflight
  (NVMe-oF classified remote+active, posture quorate) → ordered plan → dry-run
  execute job (live re-check skipped already-running guests). No VM mutated.

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
