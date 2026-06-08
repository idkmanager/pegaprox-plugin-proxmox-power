# Proxmox VM Power Control — PegaProx Plugin

> Codename: **proxmox vm power control**

Orchestrated, **dependency-aware** power control of Proxmox VMs/LXC across a
cluster, exposed as a [PegaProx](https://github.com/PegaProx/project-pegaprox)
plugin.

Proxmox's built-in `onboot` / `startup` knobs only express a *per-node* boot
order. This plugin models an explicit **cross-node dependency graph**
(`depends_on` + `order`/`suborder`), runs the operator's **pre-flight checklist**
before touching anything, and **health-gates** every start/stop step.

It reuses PegaProx's already-authenticated cluster manager session — no extra
credentials — exactly like the bundled `proxmox-ha` plugin.

## What it does

The engine follows the operator runbook:

1. Validate cluster / host availability (loop until online or timeout)
   1. Check node maintenance status
2. Validate storage availability (NFS / iSCSI / CIFS / NVMe-oF / local …)
3. Validate VM/CT boot settings (`onboot`, `startup` order)
4. Validate storage type (**local** vs **remote/shared**)
5. Validate cluster posture (**master/quorate** vs **standalone**)
6. Re-check storage availability (loop)
7. When storage is available → proceed to **Start**
8. **Start** VM/CT — ordered, local/remote branch, health-gated
9. **Stop** VM/CT — reverse order, graceful `shutdown` vs hard `stop`

Every VM/CT is controlled with **inter-VM dependency and suborder**: a
topological sort over `depends_on`, tie-broken by `order` then `suborder`.

## Safety

- **Dry-run by default.** `execute` only performs real power actions when the
  request carries `confirm: true`; otherwise it simulates and reports the plan.
- **RBAC.** Read endpoints require `vm.view`; `execute` and `config/save`
  require `vm.power` (admins always pass).
- **Storage gate.** A guest on a storage that never becomes active is never
  powered on — the step fails closed.
- **Audit.** Every config change and execution is written to the PegaProx audit
  log.

## API

All routes are dispatched under `/api/plugins/proxmox-power/api/<path>`:

| Method | Path | Perm | Purpose |
|---|---|---|---|
| GET | `ui` | — | Dashboard |
| GET | `clusters` | `vm.view` | Clusters the user may operate on |
| GET | `inventory?cluster_id=` | `vm.view` | Live VM/CT inventory |
| GET | `config` | `vm.view` | Dependency-group config |
| POST | `config/save` | `vm.power` | Persist groups (validates order/cycles) |
| POST | `preflight` | `vm.view` | Run the pre-flight checklist |
| POST | `plan` | `vm.view` | Ordered start/stop plan (no side effects) |
| POST | `execute` | `vm.power` | Run the plan (`confirm:true` = real) |
| GET | `job?id=` | `vm.view` | Live status of a job |
| GET | `jobs` | `vm.view` | Recent jobs |

`plan` / `execute` body: `{ "cluster_id": "...", "group": "...", "action": "start|stop", "confirm": false }`

## Configuration

See [`config.example.json`](config.example.json). A group:

```jsonc
{
  "id": "core-infra",
  "name": "Core Infrastructure",
  "settings": { "stop_mode": "shutdown", "step_timeout_sec": 300,
                "poll_interval_sec": 3, "storage_wait_sec": 120,
                "continue_on_error": false },
  "members": [
    { "vmid": 110, "order": 10, "health": { "mode": "status", "delay_sec": 5 } },
    { "vmid": 100, "order": 20, "depends_on": [110], "health": { "mode": "agent" } }
  ]
}
```

Health modes: `agent` (qemu guest-agent ping), `status` (poll until `running`),
`delay` (fixed wait). `delay_sec` adds a settle delay after the guest is up.

## Install

```bash
sudo bash install.sh         # on the PegaProx host
```

The installer copies the files and restarts PegaProx. It tries to enable the
plugin automatically, but **on instances with an encrypted DB (dbcrypto /
SQLCipher) it cannot** — an external `sqlite3` fails with *"file is not a
database (26)"*. That's expected; just enable it from the web UI:

> **PegaProx → Settings → Plugins → "Proxmox VM Power Control" → Enable**

Manual install: copy the plugin dir to `/opt/PegaProx/plugins/proxmox-power/`,
`echo '{ "groups": [] }' > config.json`, `systemctl restart pegaprox`, then
enable it from Settings → Plugins (works regardless of DB encryption).

## Develop

```bash
python -m pytest        # unit + engine suite (no PegaProx/Proxmox needed)
```

## License

MIT © IDKMANAGER. Co-authored with Carlos Montalvo ([@UltraHKR](https://github.com/UltraHKR)).
