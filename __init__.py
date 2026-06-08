# -*- coding: utf-8 -*-
"""
Proxmox VM Power Control — PegaProx Plugin
Codename: proxmox vm power control

Orchestrated, dependency-aware power control of Proxmox VMs/LXC across a cluster.

Where the built-in ``onboot``/``startup`` knobs only express a *per-node* boot
order, this plugin models an explicit cross-node dependency graph (``depends_on``
+ ``order``/``suborder``) and gates every step behind real health checks. It also
runs the operator's pre-flight checklist before touching anything:

  1.  Validate cluster / host availability (loop until online or timeout)
  1.1 Check node maintenance status
  2.  Validate storage availability (NFS / iSCSI / CIFS / NVMe-oF / local ...)
  3.  Validate VM/CT boot settings (onboot, startup order)
  4.  Validate storage type (local vs remote/shared)
  5.  Validate cluster posture (master/quorate vs standalone)
  6.  Re-check storage availability (loop)
  7.  When storage is available -> proceed to Start
  8.  Start VM/CT (ordered; local vs remote branch; health-gated)
  9.  Stop VM/CT (reverse order; graceful shutdown vs hard stop)

It reuses PegaProx's already-authenticated cluster manager session — no extra
credentials, no second source of truth — exactly like the bundled ``proxmox-ha``
plugin (manager._api_get/_api_post against https://<host>:8006/api2/json).

Routes (dispatched by the PegaProx catch-all, all under
``/api/plugins/proxmox-power/api/<path>``):

  GET  ui                      -> serve the dashboard
  GET  clusters                -> clusters the user may operate on
  GET  inventory?cluster_id=   -> live VM/CT inventory (vmid, node, type, status...)
  GET  config                  -> dependency-group config (admin)
  POST config/save             -> persist dependency-group config (admin)
  POST preflight               -> run the pre-flight checklist for a group
  POST plan                    -> compute the ordered start/stop plan (no side effects)
  POST execute                 -> run the plan (dry-run unless confirm=true) (vm.power)
  GET  job?id=                 -> live status of an execution job
  GET  jobs                    -> recent execution jobs

Author: IDKMANAGER
License: MIT
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone

from flask import request, jsonify, send_file

from pegaprox.api.plugins import register_plugin_route
from pegaprox.api.helpers import (
    get_connected_manager,
    check_cluster_access,
    safe_error,
)
from pegaprox.utils.auth import load_users
from pegaprox.utils.rbac import has_permission
from pegaprox.utils.audit import log_audit

PLUGIN_ID = 'proxmox-power'
PLUGIN_NAME = 'Proxmox VM Power Control'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, 'config.json')
log = logging.getLogger(f'plugin.{PLUGIN_ID}')

# Permissions (aligned with the built-in Proxmox VM API).
PERM_VIEW = 'vm.view'
PERM_POWER = 'vm.power'

# Storage backends that live on shared/remote infrastructure. Anything not in
# this set (and without Proxmox's ``shared`` flag) is treated as node-local.
REMOTE_STORAGE_TYPES = {
    'nfs', 'cifs', 'iscsi', 'iscsidirect', 'pbs', 'glusterfs',
    'cephfs', 'rbd', 'zfs',  # zfs-over-iscsi
}

# Config keys whose value carries a "<storage>:<volume>" reference.
_DISK_KEY_RE = re.compile(
    r'^(?:ide|sata|scsi|virtio|efidisk|tpmstate|rootfs|mp|unused)\d*$'
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_GROUP_SETTINGS = {
    'stop_mode': 'shutdown',     # 'shutdown' (graceful) | 'stop' (hard)
    'step_timeout_sec': 300,     # max wait for a single member to become healthy
    'poll_interval_sec': 3,      # how often to poll status while waiting
    'storage_wait_sec': 120,     # max wait for a storage to become active
    'continue_on_error': False,  # abort the run on the first failed step
}


def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('config root must be an object')
            cfg.setdefault('groups', [])
            return cfg
    except FileNotFoundError:
        return {'groups': []}
    except Exception as e:
        log.warning(f'[{PLUGIN_ID}] config load failed: {e}')
        return {'groups': []}


def _save_config(cfg):
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _get_group(cfg, group_id):
    for g in cfg.get('groups', []):
        if g.get('id') == group_id:
            return g
    return None


def _group_settings(group):
    s = dict(_DEFAULT_GROUP_SETTINGS)
    s.update(group.get('settings', {}) or {})
    return s


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without Flask / PegaProx)
# ---------------------------------------------------------------------------

def classify_storage(entry):
    """Classify a Proxmox storage status entry as 'remote' or 'local'.

    ``entry`` is a dict from GET /nodes/<node>/storage. A truthy ``shared``
    flag is authoritative (covers NVMe-oF/LVM-over-shared); otherwise we fall
    back to the storage ``type``.
    """
    if not isinstance(entry, dict):
        return 'local'
    if entry.get('shared') in (1, True, '1'):
        return 'remote'
    return 'remote' if str(entry.get('type', '')).lower() in REMOTE_STORAGE_TYPES else 'local'


def storage_available(entry):
    """A storage is usable when it is both enabled and active."""
    if not isinstance(entry, dict):
        return False
    enabled = entry.get('enabled', 1) in (1, True, '1')
    active = entry.get('active', 0) in (1, True, '1')
    return enabled and active


def extract_vm_storages(cfg):
    """Return the set of storage ids referenced by a VM/CT config dict.

    Skips empty/`none` disks and cdrom media so we only validate storages that
    actually back a disk the guest needs to boot.
    """
    storages = set()
    if not isinstance(cfg, dict):
        return storages
    for key, val in cfg.items():
        if not _DISK_KEY_RE.match(str(key)):
            continue
        sval = str(val)
        if not sval or sval.lower() == 'none':
            continue
        if 'media=cdrom' in sval:
            continue
        if ':' not in sval:
            continue
        storage_id = sval.split(':', 1)[0].strip()
        # A bare "<size>" (e.g. ide2: none) or numeric-only token is not a storage.
        if storage_id and not storage_id.isdigit():
            storages.add(storage_id)
    return storages


def parse_startup(cfg):
    """Parse the Proxmox ``startup`` string (e.g. 'order=3,up=30,down=60')."""
    out = {'order': None, 'up': None, 'down': None}
    if not isinstance(cfg, dict):
        return out
    raw = cfg.get('startup')
    if not raw:
        return out
    for part in str(raw).split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            k = k.strip()
            if k in out:
                try:
                    out[k] = int(v)
                except ValueError:
                    out[k] = v
    return out


def topo_order(members):
    """Topologically order group members for *start*.

    Ordering key, in priority:
      1. ``depends_on`` edges (a member starts after everything it depends on)
      2. explicit ``order`` (ascending)
      3. ``suborder`` (ascending, tie-break within the same order)
      4. ``vmid`` (stable final tie-break)

    Returns the ordered list of member dicts. Raises ``ValueError`` on an
    unknown dependency or a dependency cycle.
    """
    by_id = {}
    for m in members:
        vmid = int(m['vmid'])
        by_id[vmid] = m
    # Validate edges
    for m in members:
        for dep in (m.get('depends_on') or []):
            if int(dep) not in by_id:
                raise ValueError(
                    f"member {m['vmid']} depends_on unknown vmid {dep}"
                )

    def sort_key(vmid):
        m = by_id[vmid]
        order = m.get('order')
        order = order if isinstance(order, int) else 1_000_000
        sub = m.get('suborder')
        sub = sub if isinstance(sub, int) else 0
        return (order, sub, vmid)

    # Kahn's algorithm with a deterministic tie-break by sort_key.
    indeg = {vid: 0 for vid in by_id}
    adj = {vid: [] for vid in by_id}
    for m in members:
        vid = int(m['vmid'])
        for dep in (m.get('depends_on') or []):
            adj[int(dep)].append(vid)
            indeg[vid] += 1

    ready = sorted([v for v, d in indeg.items() if d == 0], key=sort_key)
    ordered = []
    while ready:
        cur = ready.pop(0)
        ordered.append(by_id[cur])
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=sort_key)

    if len(ordered) != len(by_id):
        cyclic = [v for v, d in indeg.items() if d > 0]
        raise ValueError(f'dependency cycle among vmids {sorted(cyclic)}')
    return ordered


def build_plan(group, inventory, vm_configs, storage_by_node, action):
    """Build an ordered, side-effect-free execution plan.

    Args:
      group:          a config group dict (members + settings)
      inventory:      {vmid(int): {node, name, type('qemu'|'lxc'), status}}
      vm_configs:     {vmid(int): raw config dict} (for storage + boot settings)
      storage_by_node:{node: {storage_id: status_entry}}
      action:         'start' | 'stop'

    Returns a list of step dicts. For 'stop' the order is reversed so that
    dependents stop before their dependencies.
    """
    members = group.get('members', [])
    ordered = topo_order(members)
    if action == 'stop':
        ordered = list(reversed(ordered))

    settings = _group_settings(group)
    steps = []
    for m in ordered:
        vmid = int(m['vmid'])
        inv = inventory.get(vmid, {})
        node = inv.get('node')
        vtype = inv.get('type')  # 'qemu' | 'lxc'
        cfg = vm_configs.get(vmid, {})
        needed = extract_vm_storages(cfg)

        # Classify each backing storage local/remote and check availability.
        storage_report = []
        worst = 'ok'
        placement = 'local'
        node_stores = storage_by_node.get(node, {})
        for sid in sorted(needed):
            entry = node_stores.get(sid)
            kind = classify_storage(entry) if entry else 'unknown'
            avail = storage_available(entry) if entry else False
            if kind == 'remote':
                placement = 'remote'
            if not avail:
                worst = 'unavailable'
            storage_report.append({
                'storage': sid, 'kind': kind, 'available': avail,
            })

        present = vmid in inventory
        cur_status = inv.get('status', 'unknown')
        if action == 'start':
            noop = cur_status == 'running'
        else:
            noop = cur_status in ('stopped', 'unknown') and present

        steps.append({
            'vmid': vmid,
            'name': m.get('name') or inv.get('name') or str(vmid),
            'node': node,
            'type': vtype,
            'action': action,
            'placement': placement,          # local | remote (storage-derived)
            'current_status': cur_status,
            'present': present,
            'noop': noop,
            'depends_on': [int(d) for d in (m.get('depends_on') or [])],
            'order': m.get('order'),
            'suborder': m.get('suborder'),
            'startup': parse_startup(cfg),
            'storage': storage_report,
            'storage_state': worst,          # ok | unavailable
            'health': m.get('health') or {'mode': 'status'},
            'stop_mode': settings['stop_mode'],
        })
    return steps


# ---------------------------------------------------------------------------
# Proxmox access (thin wrappers over the authenticated manager)
# ---------------------------------------------------------------------------

def _px(manager, path):
    return f'https://{manager.host}:{manager.api_port}/api2/json{path}'


def _get_json(manager, path):
    """GET and return parsed ``data`` (or None on non-200)."""
    r = manager._api_get(_px(manager, path))
    if r.status_code != 200:
        return None
    try:
        return r.json().get('data')
    except ValueError:
        return None


def _ep(vtype):
    """Map an inventory type to the Proxmox endpoint segment."""
    return 'lxc' if vtype == 'lxc' else 'qemu'


def fetch_inventory(manager):
    """Return {vmid:int -> {node,name,type,status}} from /cluster/resources."""
    data = _get_json(manager, '/cluster/resources?type=vm') or []
    inv = {}
    for r in data:
        try:
            vmid = int(r.get('vmid'))
        except (TypeError, ValueError):
            continue
        inv[vmid] = {
            'node': r.get('node'),
            'name': r.get('name'),
            'type': r.get('type'),   # 'qemu' | 'lxc'
            'status': r.get('status', 'unknown'),
        }
    return inv


def fetch_nodes(manager):
    """Return {node -> status_entry} from /nodes."""
    data = _get_json(manager, '/nodes') or []
    return {n.get('node'): n for n in data if n.get('node')}


def fetch_storage_for_node(manager, node):
    """Return {storage_id -> status_entry} from /nodes/<node>/storage."""
    data = _get_json(manager, f'/nodes/{node}/storage') or []
    return {s.get('storage'): s for s in data if s.get('storage')}


def fetch_vm_config(manager, node, vtype, vmid):
    return _get_json(manager, f'/nodes/{node}/{_ep(vtype)}/{vmid}/config') or {}


def fetch_vm_status(manager, node, vtype, vmid):
    return _get_json(manager, f'/nodes/{node}/{_ep(vtype)}/{vmid}/status/current') or {}


def fetch_cluster_posture(manager):
    """Return {'mode': 'cluster'|'standalone', 'quorate': bool, 'nodes': [...]}.

    A single-node install reports no cluster node entries -> standalone.
    """
    data = _get_json(manager, '/cluster/status') or []
    cluster_entry = next((d for d in data if d.get('type') == 'cluster'), None)
    node_entries = [d for d in data if d.get('type') == 'node']
    if cluster_entry:
        quorate = cluster_entry.get('quorate') in (1, True, '1')
        return {
            'mode': 'cluster',
            'quorate': quorate,
            'nodes': [
                {'name': n.get('name'), 'online': n.get('online') in (1, True, '1'),
                 'local': n.get('local') in (1, True, '1')}
                for n in node_entries
            ],
        }
    return {'mode': 'standalone', 'quorate': True, 'nodes': node_entries}


def _agent_ping(manager, node, vmid):
    """Return True if the qemu guest agent answers a ping."""
    try:
        r = manager._api_post(_px(manager, f'/nodes/{node}/qemu/{vmid}/agent/ping'))
        return r.status_code == 200
    except Exception:
        return False


def _power(manager, node, vtype, vmid, verb):
    """POST a power verb; return (ok, detail)."""
    r = manager._api_post(_px(manager, f'/nodes/{node}/{_ep(vtype)}/{vmid}/status/{verb}'))
    if r.status_code == 200:
        return True, None
    detail = None
    try:
        j = r.json()
        detail = j.get('errors') or j.get('message')
    except ValueError:
        pass
    return False, detail or (r.text or f'HTTP {r.status_code}')


# ---------------------------------------------------------------------------
# Pre-flight checklist (spec steps 1-7)
# ---------------------------------------------------------------------------

def run_preflight(manager, group, inventory):
    """Execute the operator checklist and return a structured report."""
    members = group.get('members', [])
    posture = fetch_cluster_posture(manager)
    nodes = fetch_nodes(manager)

    checks = []

    # 5. master/standalone posture
    checks.append({
        'id': 'cluster_posture', 'ok': posture.get('quorate', False),
        'detail': f"mode={posture['mode']} quorate={posture.get('quorate')}",
    })

    needed_nodes = sorted({inventory.get(int(m['vmid']), {}).get('node')
                           for m in members if int(m['vmid']) in inventory})
    needed_nodes = [n for n in needed_nodes if n]

    # 1 / 1.1 node availability + maintenance
    for node in needed_nodes:
        entry = nodes.get(node, {})
        online = entry.get('status') == 'online'
        checks.append({
            'id': f'node:{node}', 'ok': online,
            'detail': f"status={entry.get('status', 'unknown')}",
        })

    # 2 / 4 / 6 storage availability + type per member
    storage_by_node = {n: fetch_storage_for_node(manager, n) for n in needed_nodes}
    missing_members = []
    for m in members:
        vmid = int(m['vmid'])
        if vmid not in inventory:
            missing_members.append(vmid)
            checks.append({'id': f'vm:{vmid}', 'ok': False, 'detail': 'not found in cluster'})
            continue
        inv = inventory[vmid]
        cfg = fetch_vm_config(manager, inv['node'], inv['type'], vmid)
        startup = parse_startup(cfg)
        for sid in sorted(extract_vm_storages(cfg)):
            entry = storage_by_node.get(inv['node'], {}).get(sid)
            ok = storage_available(entry)
            kind = classify_storage(entry) if entry else 'unknown'
            checks.append({
                'id': f'storage:{vmid}:{sid}', 'ok': ok,
                'detail': f"kind={kind} active={storage_available(entry)}",
            })
        # 3. boot settings (informational, never fails preflight)
        checks.append({
            'id': f'boot:{vmid}', 'ok': True,
            'detail': f"onboot={cfg.get('onboot', 0)} startup_order={startup['order']}",
        })

    overall = all(c['ok'] for c in checks)
    return {
        'ok': overall,
        'posture': posture,
        'checks': checks,
        'missing_members': missing_members,
    }


# ---------------------------------------------------------------------------
# Execution engine (jobs)
# ---------------------------------------------------------------------------

_jobs = {}
_jobs_lock = threading.Lock()
_JOB_RETENTION = 25
_job_seq = 0


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _new_job(cluster_id, group_id, action, dry_run, username):
    global _job_seq
    with _jobs_lock:
        _job_seq += 1
        job_id = f'job-{_job_seq}'
        _jobs[job_id] = {
            'id': job_id, 'cluster_id': cluster_id, 'group': group_id,
            'action': action, 'dry_run': dry_run, 'user': username,
            'status': 'running', 'started': _now_iso(), 'finished': None,
            'steps': [], 'log': [],
        }
        # Trim history
        if len(_jobs) > _JOB_RETENTION:
            for old in sorted(_jobs, key=lambda k: _jobs[k]['started'])[:-_JOB_RETENTION]:
                if _jobs[old]['status'] != 'running':
                    _jobs.pop(old, None)
        return _jobs[job_id]


def _job_log(job, msg):
    job['log'].append({'ts': _now_iso(), 'msg': msg})
    log.info(f"[{PLUGIN_ID}] {job['id']}: {msg}")


def _wait_storage(manager, node, storages, timeout_sec, poll):
    """Loop until every storage on a node is active, or timeout."""
    deadline = time.time() + timeout_sec
    while True:
        live = fetch_storage_for_node(manager, node)
        pending = [s for s in storages if not storage_available(live.get(s))]
        if not pending:
            return True, None
        if time.time() >= deadline:
            return False, f"storage not active: {', '.join(pending)}"
        time.sleep(poll)


def _wait_health(manager, node, vtype, vmid, health, timeout_sec, poll):
    """Wait until a started guest is healthy per the configured mode."""
    mode = (health or {}).get('mode', 'status')
    delay = int((health or {}).get('delay_sec', 0) or 0)
    deadline = time.time() + timeout_sec
    if mode == 'delay':
        time.sleep(min(delay, max(0, deadline - time.time())))
        return True, None
    while True:
        st = fetch_vm_status(manager, node, vtype, vmid)
        running = st.get('status') == 'running'
        if running and mode == 'agent' and vtype == 'qemu':
            if _agent_ping(manager, node, vmid):
                if delay:
                    time.sleep(delay)
                return True, None
        elif running:
            if delay:
                time.sleep(delay)
            return True, None
        if time.time() >= deadline:
            return False, f'timeout waiting for {mode}'
        time.sleep(poll)


def _wait_stopped(manager, node, vtype, vmid, timeout_sec, poll):
    deadline = time.time() + timeout_sec
    while True:
        st = fetch_vm_status(manager, node, vtype, vmid)
        if st.get('status') == 'stopped':
            return True, None
        if time.time() >= deadline:
            return False, 'timeout waiting for stop'
        time.sleep(poll)


def _execute_job(job, manager, group, inventory, steps):
    settings = _group_settings(group)
    poll = settings['poll_interval_sec']
    step_timeout = settings['step_timeout_sec']
    storage_wait = settings['storage_wait_sec']
    dry = job['dry_run']
    action = job['action']
    failed = False

    for step in steps:
        rec = dict(step)
        rec['state'] = 'pending'
        rec['detail'] = None
        rec['ts'] = _now_iso()
        job['steps'].append(rec)

        vmid, node, vtype = step['vmid'], step['node'], step['type']
        if not step['present']:
            rec['state'] = 'skipped'
            rec['detail'] = 'not found in cluster'
            _job_log(job, f"{vmid}: skipped (missing)")
            continue
        if step['noop']:
            rec['state'] = 'skipped'
            rec['detail'] = f"already {step['current_status']}"
            _job_log(job, f"{vmid}: skipped (already {step['current_status']})")
            continue

        if dry:
            rec['state'] = 'simulated'
            verb = 'start' if action == 'start' else step['stop_mode']
            rec['detail'] = (f"would {verb} {vtype}/{vmid} on {node} "
                             f"[{step['placement']}], then wait "
                             f"({step['health'].get('mode', 'status')})")
            _job_log(job, f"{vmid}: {rec['detail']}")
            continue

        try:
            if action == 'start':
                # storage gate (loop) — spec steps 2/6/7, local vs remote branch
                stores = [s['storage'] for s in step['storage']]
                if stores:
                    ok, err = _wait_storage(manager, node, stores, storage_wait, poll)
                    if not ok:
                        raise RuntimeError(err)
                ok, err = _power(manager, node, vtype, vmid, 'start')
                if not ok:
                    raise RuntimeError(f'start failed: {err}')
                ok, err = _wait_health(manager, node, vtype, vmid,
                                       step['health'], step_timeout, poll)
                if not ok:
                    raise RuntimeError(err)
                rec['state'] = 'done'
                rec['detail'] = 'running + healthy'
            else:
                verb = step['stop_mode']  # 'shutdown' | 'stop'
                ok, err = _power(manager, node, vtype, vmid, verb)
                if not ok:
                    raise RuntimeError(f'{verb} failed: {err}')
                ok, err = _wait_stopped(manager, node, vtype, vmid, step_timeout, poll)
                if not ok:
                    raise RuntimeError(err)
                rec['state'] = 'done'
                rec['detail'] = 'stopped'
            _job_log(job, f"{vmid}: {rec['state']} ({rec['detail']})")
        except Exception as e:
            rec['state'] = 'failed'
            rec['detail'] = str(e)
            failed = True
            _job_log(job, f"{vmid}: FAILED {e}")
            if not settings['continue_on_error']:
                break

    job['status'] = 'failed' if failed else 'done'
    job['finished'] = _now_iso()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _current_user():
    users = load_users()
    return users.get(request.session.get('user'), {})


def _username():
    return request.session.get('user', 'system')


def _require(perm):
    if not has_permission(_current_user(), perm):
        return jsonify({'error': 'Permission denied', 'required': perm}), 403
    return None


def _resolve_cluster():
    """Return (cluster_id, manager, None) or (None, None, error_response)."""
    cluster_id = (request.args.get('cluster_id')
                  or (request.get_json(silent=True) or {}).get('cluster_id') or '').strip()
    if not cluster_id:
        return None, None, (jsonify({'error': 'cluster_id is required'}), 400)
    allowed, err = check_cluster_access(cluster_id)
    if not allowed:
        return None, None, err
    manager, err = get_connected_manager(cluster_id)
    if err:
        return None, None, err
    return cluster_id, manager, None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def serve_ui():
    html = os.path.join(PLUGIN_DIR, 'power.html')
    if os.path.exists(html):
        return send_file(html, mimetype='text/html')
    return jsonify({'error': 'UI not found'}), 404


def clusters_handler():
    if (err := _require(PERM_VIEW)):
        return err
    from pegaprox.globals import cluster_managers
    out = []
    for cid, mgr in cluster_managers.items():
        allowed, _ = check_cluster_access(cid)
        if not allowed:
            continue
        out.append({
            'id': cid,
            'name': getattr(mgr, 'name', cid),
            'connected': getattr(mgr, 'is_connected', False),
        })
    return jsonify({'clusters': out})


def inventory_handler():
    if (err := _require(PERM_VIEW)):
        return err
    cluster_id, manager, err = _resolve_cluster()
    if err:
        return err
    try:
        inv = fetch_inventory(manager)
    except Exception as e:
        return jsonify({'error': safe_error(e, 'inventory failed')}), 500
    items = [dict(vmid=k, **v) for k, v in sorted(inv.items())]
    return jsonify({'cluster_id': cluster_id, 'count': len(items), 'vms': items})


def config_handler():
    if (err := _require(PERM_VIEW)):
        return err
    return jsonify(_load_config())


def config_save_handler():
    if (err := _require(PERM_POWER)):
        return err
    body = request.get_json(silent=True) or {}
    groups = body.get('groups')
    if not isinstance(groups, list):
        return jsonify({'error': "body.groups must be a list"}), 400
    # Validate each group is internally consistent (orderable, no cycles).
    for g in groups:
        if not g.get('id'):
            return jsonify({'error': 'each group needs an id'}), 400
        try:
            topo_order(g.get('members', []))
        except ValueError as e:
            return jsonify({'error': f"group '{g.get('id')}': {e}"}), 400
    cfg = _load_config()
    cfg['groups'] = groups
    _save_config(cfg)
    log_audit(user=_username(), action='power.config_saved',
              details=f'{len(groups)} group(s)')
    return jsonify({'ok': True, 'groups': len(groups)})


def _load_group_or_error(cfg_group_id):
    cfg = _load_config()
    group = _get_group(cfg, cfg_group_id)
    if not group:
        return None, (jsonify({'error': f"group '{cfg_group_id}' not found"}), 404)
    return group, None


def preflight_handler():
    if (err := _require(PERM_VIEW)):
        return err
    cluster_id, manager, err = _resolve_cluster()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    group, err = _load_group_or_error(body.get('group'))
    if err:
        return err
    try:
        inv = fetch_inventory(manager)
        report = run_preflight(manager, group, inv)
    except Exception as e:
        return jsonify({'error': safe_error(e, 'preflight failed')}), 500
    return jsonify({'cluster_id': cluster_id, 'group': group['id'], **report})


def _collect_plan_inputs(manager, group, inv):
    """Gather configs + per-node storage needed to build a plan."""
    vm_configs, nodes_needed = {}, set()
    for m in group.get('members', []):
        vmid = int(m['vmid'])
        if vmid in inv:
            n = inv[vmid]
            nodes_needed.add(n['node'])
            vm_configs[vmid] = fetch_vm_config(manager, n['node'], n['type'], vmid)
    storage_by_node = {n: fetch_storage_for_node(manager, n) for n in nodes_needed}
    return vm_configs, storage_by_node


def plan_handler():
    if (err := _require(PERM_VIEW)):
        return err
    cluster_id, manager, err = _resolve_cluster()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    action = body.get('action', 'start')
    if action not in ('start', 'stop'):
        return jsonify({'error': "action must be 'start' or 'stop'"}), 400
    group, err = _load_group_or_error(body.get('group'))
    if err:
        return err
    try:
        inv = fetch_inventory(manager)
        vm_configs, storage_by_node = _collect_plan_inputs(manager, group, inv)
        steps = build_plan(group, inv, vm_configs, storage_by_node, action)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': safe_error(e, 'plan failed')}), 500
    return jsonify({'cluster_id': cluster_id, 'group': group['id'],
                    'action': action, 'steps': steps})


def execute_handler():
    if (err := _require(PERM_POWER)):
        return err
    cluster_id, manager, err = _resolve_cluster()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    action = body.get('action', 'start')
    if action not in ('start', 'stop'):
        return jsonify({'error': "action must be 'start' or 'stop'"}), 400
    # Safety: real execution requires an explicit confirm; otherwise dry-run.
    dry_run = not (body.get('confirm') is True)
    group, err = _load_group_or_error(body.get('group'))
    if err:
        return err
    try:
        inv = fetch_inventory(manager)
        vm_configs, storage_by_node = _collect_plan_inputs(manager, group, inv)
        steps = build_plan(group, inv, vm_configs, storage_by_node, action)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': safe_error(e, 'plan failed')}), 500

    job = _new_job(cluster_id, group['id'], action, dry_run, _username())
    log_audit(user=_username(),
              action=f'power.{action}' + ('_dryrun' if dry_run else ''),
              details=f"group={group['id']} steps={len(steps)} dry_run={dry_run}",
              cluster=cluster_id)
    t = threading.Thread(
        target=_execute_job, args=(job, manager, group, inv, steps),
        daemon=True, name=f'power-{job["id"]}')
    t.start()
    return jsonify({'job_id': job['id'], 'dry_run': dry_run, 'steps': len(steps)})


def job_handler():
    if (err := _require(PERM_VIEW)):
        return err
    job_id = request.args.get('id', '').strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'job not found'}), 404
    return jsonify(job)


def jobs_handler():
    if (err := _require(PERM_VIEW)):
        return err
    with _jobs_lock:
        items = sorted(_jobs.values(), key=lambda j: j['started'], reverse=True)
        summary = [{k: j[k] for k in
                    ('id', 'cluster_id', 'group', 'action', 'dry_run',
                     'status', 'started', 'finished', 'user')} for j in items]
    return jsonify({'jobs': summary})


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(app=None):
    routes = {
        'ui': serve_ui,
        'clusters': clusters_handler,
        'inventory': inventory_handler,
        'config': config_handler,
        'config/save': config_save_handler,
        'preflight': preflight_handler,
        'plan': plan_handler,
        'execute': execute_handler,
        'job': job_handler,
        'jobs': jobs_handler,
    }
    for path, handler in routes.items():
        register_plugin_route(PLUGIN_ID, path, handler)
    log.info(f"[{PLUGIN_ID}] Registered {len(routes)} routes")
