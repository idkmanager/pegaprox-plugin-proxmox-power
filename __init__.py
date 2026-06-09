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
  GET  autostart/config        -> unattended-boot settings + last-run state
  POST autostart/save          -> persist unattended-boot settings (vm.power)
  POST autostart/run           -> trigger the autostart groups now (dry-run unless confirm)

Author: IDKMANAGER
License: MIT
"""

import os
import re
import json
import time
import shutil
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
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
PLUGIN_NAME = 'Powvm Control'
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

# Config keys whose value carries a "<storage>:<volume>" reference that the
# guest needs *to boot*. Deliberately excludes ``unused<N>`` (detached disks):
# Proxmox does not require a detached volume's storage to be active to start the
# guest, so gating start on it would be wrongly over-strict.
_DISK_KEY_RE = re.compile(
    r'^(?:ide|sata|scsi|virtio|efidisk|tpmstate|rootfs|mp)\d*$'
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_GROUP_SETTINGS = {
    'stop_mode': 'shutdown',     # 'shutdown' (graceful) | 'stop' (hard)
    'step_timeout_sec': 300,     # max wait for a single member to become healthy
    'poll_interval_sec': 3,      # how often to poll status while waiting
    'storage_wait_sec': 120,     # max wait for a storage to become active (per member, spec 6)
    'storage_ready_sec': 0,      # wait for ALL backing storage to be live before the
                                 # sequence even starts (spec 6/7; 0 = off, rely on per-member gate)
    'host_wait_sec': 60,         # max wait for the target node to be online (spec 1)
    'ignore_maintenance': False, # if True, act even when the node is in HA maintenance (spec 1.1)
    'continue_on_error': False,  # abort the run on the first failed step
}

# Per-member storage policy when a backing storage is not active at start time.
STORAGE_POLICIES = ('wait', 'fail', 'skip')


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


def _reconcile_autostart_groups(cfg):
    """Drop autostart.groups entries that don't match an existing group id.

    A dangling reference (a group that was deleted or recreated with a new id)
    can only ever be a silent no-op at boot ("group not found"), so it must
    never linger in the config. Mutates ``cfg`` in place and returns the list of
    pruned ids (empty when nothing changed)."""
    a = cfg.get('autostart')
    if not isinstance(a, dict) or not isinstance(a.get('groups'), list):
        return []
    known = {g.get('id') for g in cfg.get('groups', [])}
    pruned = [gid for gid in a['groups'] if gid not in known]
    if pruned:
        a['groups'] = [gid for gid in a['groups'] if gid in known]
    return pruned


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


def storage_type_label(entry):
    """Human label for a storage backend (NFS / iSCSI / CIFS / NVMe-oF / …).

    NVMe-oF on this fleet surfaces as a *shared* LVM, so lvm+shared is labelled
    NVMe-oF/Shared-LVM. Used to make the pre-flight storage validation readable.
    """
    if not isinstance(entry, dict):
        return 'unknown'
    t = str(entry.get('type', '')).lower()
    shared = entry.get('shared') in (1, True, '1')
    names = {
        'nfs': 'NFS', 'cifs': 'CIFS/SMB', 'iscsi': 'iSCSI', 'iscsidirect': 'iSCSI',
        'pbs': 'PBS', 'glusterfs': 'GlusterFS', 'cephfs': 'CephFS', 'rbd': 'Ceph/RBD',
        'zfs': 'ZFS-over-iSCSI', 'zfspool': 'ZFS', 'lvmthin': 'LVM-Thin',
        'lvm': 'LVM', 'dir': 'Directory', 'btrfs': 'Btrfs',
    }
    label = names.get(t, t or 'unknown')
    if t == 'lvm' and shared:
        label = 'NVMe-oF / Shared-LVM'
    elif shared and t not in ('nfs', 'cifs', 'iscsi', 'pbs', 'glusterfs', 'cephfs', 'rbd'):
        label += ' (shared)'
    return label


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


def build_plan(group, inventory, vm_configs, storage_by_node, action, storage_defs=None):
    """Build an ordered, side-effect-free execution plan.

    Args:
      group:          a config group dict (members + settings)
      inventory:      {vmid(int): {node, name, type('qemu'|'lxc'), status}}
      vm_configs:     {vmid(int): raw config dict} (for storage + boot settings)
      storage_by_node:{node: {storage_id: status_entry}}  (live per-node status)
      action:         'start' | 'stop'
      storage_defs:   {storage_id: cluster-level def}  (classification fallback
                      when a storage isn't active/listed on the node yet)

    Returns a list of step dicts. For 'stop' the order is reversed so that
    dependents stop before their dependencies.
    """
    storage_defs = storage_defs or {}
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
            node_entry = node_stores.get(sid)
            # Classify from the live per-node status if present, else fall back to
            # the cluster-level storage definition (still has type/shared).
            cls_entry = node_entry or storage_defs.get(sid)
            kind = classify_storage(cls_entry) if cls_entry else 'unknown'
            # Availability is only meaningful from the live per-node status.
            avail = storage_available(node_entry) if node_entry else False
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

        policy = m.get('storage_policy', 'wait')
        if policy not in STORAGE_POLICIES:
            policy = 'wait'
        order_val = m.get('order')
        phase = order_val if isinstance(order_val, int) else 1_000_000
        steps.append({
            'vmid': vmid,
            'name': m.get('name') or inv.get('name') or str(vmid),
            'node': node,
            'type': vtype,
            'action': action,
            'phase': phase,                  # = order; same phase => parallel wave
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
            'storage_policy': policy,        # wait | fail | skip
            'health': m.get('health') or {'mode': 'status'},
            'stop_mode': settings['stop_mode'],
        })
    _assign_waves(steps)
    for s in steps:
        e, mx = estimate_step_seconds(s, settings)
        s['timing'] = {'est_sec': e, 'max_sec': mx,
                       'est_min': _minutes(e), 'max_min': _minutes(mx)}
    return steps


def _assign_waves(steps):
    """Number consecutive same-phase steps as waves and flag parallel members.

    ``steps`` is already ordered by (order, suborder, vmid) — for 'stop' it's the
    reversed list, so wave 1 is whatever runs first in that direction. Members
    sharing a phase run in parallel; waves run sequentially.
    """
    from collections import Counter
    counts = Counter(s['phase'] for s in steps)
    wave = 0
    prev = object()
    for s in steps:
        if s['phase'] != prev:
            wave += 1
            prev = s['phase']
        s['wave'] = wave
        s['parallel'] = counts[s['phase']] > 1


# ---------------------------------------------------------------------------
# Time estimation (so the operator can size a maintenance window)
# ---------------------------------------------------------------------------
# Rough per-step duration model in *seconds*. Deliberately simple: a typical
# case to gauge a window, plus a worst-case ceiling derived from the configured
# timeouts. Phases (same `order`) run in parallel -> a phase costs as long as
# its slowest member; phases run sequentially -> the run costs the sum.

_EST = {
    'power': 5,          # issuing the power verb + Proxmox task spin-up
    'boot_status': 20,   # time to reach 'running' (status health)
    'boot_agent': 45,    # time to reach guest-agent ping (services up)
    'shutdown': 30,      # graceful ACPI shutdown
    'hard_stop': 5,      # hard stop
}


def _minutes(sec):
    """Seconds -> minutes rounded to one decimal (0 stays 0)."""
    return round((sec or 0) / 60.0, 1)


def estimate_step_seconds(step, settings):
    """Return (typical_sec, worst_case_sec) for a single step.

    A no-op (already in target state) or absent member costs nothing. Worst case
    is bounded by the configured step/storage timeouts; the typical case uses a
    fixed boot/shutdown model so the estimate is deterministic and reviewable.
    """
    if not step.get('present') or step.get('noop'):
        return 0, 0
    action = step.get('action', 'start')
    health = step.get('health') or {}
    mode = health.get('mode', 'status')
    delay = int(health.get('delay_sec', 0) or 0)
    step_timeout = int(health.get('timeout_sec') or settings.get('step_timeout_sec', 300))
    power = _EST['power']
    if action == 'start':
        if mode == 'delay':
            est = mx = power + delay
        else:
            boot = _EST['boot_agent'] if mode == 'agent' else _EST['boot_status']
            est = power + boot + delay
            mx = power + step_timeout + delay
        # Worst case only: a stalled backing storage we're configured to wait for.
        if step.get('storage') and step.get('storage_policy', 'wait') == 'wait':
            mx += int(settings.get('storage_wait_sec', 120))
        return est, mx
    # stop
    base = _EST['shutdown'] if step.get('stop_mode') == 'shutdown' else _EST['hard_stop']
    return power + base, power + step_timeout


def plan_timing(steps):
    """Aggregate per-step ``timing`` into per-phase and total estimates.

    Within a phase members run in parallel (cost = slowest); phases run
    sequentially (cost = sum). Reads each step's precomputed ``timing`` dict.
    """
    order, bucket = [], {}
    for s in steps:
        w = s.get('wave')
        if w not in bucket:
            bucket[w] = []
            order.append(w)
        bucket[w].append(s)

    def _t(s):
        t = s.get('timing') or {}
        return t.get('est_sec', 0), t.get('max_sec', 0)

    phases, total_est, total_max = [], 0, 0
    for w in order:
        members = bucket[w]
        pest = max((_t(s)[0] for s in members), default=0)
        pmax = max((_t(s)[1] for s in members), default=0)
        total_est += pest
        total_max += pmax
        phases.append({
            'wave': w, 'phase': members[0].get('phase'),
            'count': len(members), 'parallel': len(members) > 1,
            'est_sec': pest, 'max_sec': pmax,
            'est_min': _minutes(pest), 'max_min': _minutes(pmax),
        })
    return {'phases': phases,
            'est_sec': total_est, 'max_sec': total_max,
            'est_min': _minutes(total_est), 'max_min': _minutes(total_max)}


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


def fetch_cluster_storage_defs(manager):
    """Return {storage_id: def} from /storage (datacenter storage.cfg).

    The cluster-level definition carries ``type``/``shared``/``nodes`` even when
    a storage isn't currently active on a given node, so it's a reliable
    classification fallback for local-vs-remote.
    """
    data = _get_json(manager, '/storage') or []
    out = {}
    for s in data:
        sid = s.get('storage')
        if sid:
            out[sid] = s
    return out


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


def fetch_ha_node_states(manager):
    """Return {node: state_string} from HA manager status (spec 1.1 maintenance).

    Reads /cluster/ha/status/manager_status. ``manager_status.node_status`` maps
    each node to 'online' | 'maintenance' | 'unknown'; ``lrm_status.<node>.mode``
    can also be 'maintenance'. Returns {} when the cluster has no HA (single
    node / non-quorate) — callers treat an absent node as 'online'.
    """
    out = {}
    data = _get_json(manager, '/cluster/ha/status/manager_status')
    if isinstance(data, dict):
        ns = (data.get('manager_status') or {}).get('node_status') or {}
        if isinstance(ns, dict):
            for n, s in ns.items():
                out[n] = s
        lrm = data.get('lrm_status') or {}
        if isinstance(lrm, dict):
            for n, info in lrm.items():
                if isinstance(info, dict) and (
                        'maintenance' in str(info.get('mode', ''))
                        or 'maintenance' in str(info.get('state', ''))):
                    out[n] = 'maintenance'
    return out


def node_in_maintenance(ha_states, node):
    return 'maintenance' in str(ha_states.get(node, ''))


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
    ha_states = fetch_ha_node_states(manager)
    storage_defs = fetch_cluster_storage_defs(manager)

    checks = []

    # 5. master/standalone posture
    checks.append({
        'id': 'cluster_posture', 'ok': posture.get('quorate', False),
        'category': 'cluster',
        'detail': f"mode={posture['mode']} quorate={posture.get('quorate')}",
    })

    needed_nodes = sorted({inventory.get(int(m['vmid']), {}).get('node')
                           for m in members if int(m['vmid']) in inventory})
    needed_nodes = [n for n in needed_nodes if n]
    needed_set = set(needed_nodes)

    # 1 / 1.1 — show availability + maintenance for the WHOLE cluster, not just
    # the nodes hosting this group's members. Only the member-hosting nodes
    # ('critical') gate the overall result; the rest are informational so a
    # node unrelated to the group doesn't turn the pre-flight red.
    for node in sorted(nodes.keys()):
        entry = nodes.get(node, {})
        online = entry.get('status') == 'online'
        relevant = node in needed_set
        suffix = ' · host del grupo' if relevant else ''
        checks.append({
            'id': f'node:{node}', 'ok': online, 'category': 'node',
            'relevant': relevant, 'critical': relevant,
            'detail': f"status={entry.get('status', 'unknown')}{suffix}",
        })
        maint = node_in_maintenance(ha_states, node)
        checks.append({
            'id': f'maint:{node}', 'ok': not maint, 'category': 'node',
            'relevant': relevant, 'critical': relevant,
            'detail': 'in HA maintenance' if maint else 'no maintenance',
        })

    # 2 / 4 / 6 storage availability + type per member
    storage_by_node = {n: fetch_storage_for_node(manager, n) for n in needed_nodes}
    missing_members = []
    for m in members:
        vmid = int(m['vmid'])
        if vmid not in inventory:
            missing_members.append(vmid)
            checks.append({'id': f'vm:{vmid}', 'ok': False, 'category': 'storage',
                           'detail': 'not found in cluster'})
            continue
        inv = inventory[vmid]
        cfg = fetch_vm_config(manager, inv['node'], inv['type'], vmid)
        startup = parse_startup(cfg)
        # 2 / 4 / 6 storage availability + type (NFS/iSCSI/CIFS/NVMe-oF/local)
        for sid in sorted(extract_vm_storages(cfg)):
            node_entry = storage_by_node.get(inv['node'], {}).get(sid)
            # Classify from live per-node status, else cluster-level def.
            cls_entry = node_entry or storage_defs.get(sid)
            ok = storage_available(node_entry)
            stype = storage_type_label(cls_entry) if cls_entry else 'unknown'
            placement = classify_storage(cls_entry) if cls_entry else 'unknown'
            avail_txt = 'active' if ok else ('INACTIVE' if node_entry else 'no status on node')
            checks.append({
                'id': f'storage:{vmid}:{sid}', 'ok': ok, 'category': 'storage',
                'vmid': vmid, 'name': inv.get('name'), 'storage': sid,
                'stype': stype, 'placement': placement, 'node': inv['node'],
                'detail': f"{stype} · {placement} · {avail_txt}",
            })
        # 3. boot settings (informational, never fails preflight)
        checks.append({
            'id': f'boot:{vmid}', 'ok': True, 'category': 'boot',
            'vmid': vmid, 'name': inv.get('name'),
            'detail': f"onboot={cfg.get('onboot', 0)} startup_order={startup['order']}",
        })

    # Only 'critical' checks gate the result. Non-critical checks (cluster nodes
    # that don't host a member) are shown for visibility but never fail it.
    overall = all(c['ok'] for c in checks if c.get('critical', True))
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
            'started_epoch': time.time(), 'elapsed_sec': None, 'elapsed_min': None,
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


def _wait_node_online(manager, node, ha_states, timeout_sec, poll, ignore_maintenance):
    """Spec 1 + 1.1: loop until the target node is online; refuse a node in HA
    maintenance unless the operator opted to ignore it."""
    if node_in_maintenance(ha_states, node) and not ignore_maintenance:
        return False, f'node {node} is in HA maintenance'
    deadline = time.time() + timeout_sec
    while True:
        status = fetch_nodes(manager).get(node, {}).get('status')
        if status == 'online':
            return True, None
        if time.time() >= deadline:
            return False, f'node {node} not online (status={status})'
        time.sleep(poll)


def _storage_gate(manager, step, settings, poll):
    """Spec 2/6/7: ensure the guest's backing storages are active, honoring the
    member's storage_policy. Returns (state, detail) where state is
    'ok' | 'skip' | 'fail'."""
    stores = [s['storage'] for s in step['storage']]
    if not stores:
        return 'ok', None
    policy = step.get('storage_policy', 'wait')
    live = fetch_storage_for_node(manager, step['node'])
    pending = [s for s in stores if not storage_available(live.get(s))]
    if not pending:
        return 'ok', None
    msg = f"storage not active: {', '.join(pending)}"
    if policy == 'fail':
        return 'fail', msg
    if policy == 'skip':
        return 'skip', msg + ' (policy=skip)'
    ok, err = _wait_storage(manager, step['node'], stores, settings['storage_wait_sec'], poll)
    return ('ok', None) if ok else ('fail', err)


def _required_storages(steps):
    """{node: set(storage_ids)} that the plan's startable members need to boot.

    Only members we intend to actually wait on (policy 'wait', present, not
    already running, on a real start) contribute — a 'skip'/'fail' member must
    not hold up the whole sequence for storage that may be offline on purpose.
    """
    req = {}
    for s in steps:
        if s.get('action') != 'start' or s.get('noop') or not s.get('present'):
            continue
        if s.get('storage_policy', 'wait') != 'wait':
            continue
        node = s.get('node')
        if not node:
            continue
        for entry in s.get('storage', []):
            req.setdefault(node, set()).add(entry['storage'])
    return req


def _wait_storage_ready(manager, steps, timeout_sec, poll, job=None):
    """Spec 6/7: before the boot sequence begins, loop until EVERY backing
    storage the plan needs is active across its node(s), or timeout.

    Returns (ok, detail). After a power event the shared backend (NVMe-oF / NFS /
    iSCSI) can take minutes to come live; this holds the whole sequence — not
    just the first VM — until storage is up. Caller decides what to do on
    timeout (we log and let the per-member gate make the final call)."""
    req = _required_storages(steps)
    if not req:
        return True, None
    deadline = time.time() + max(0, timeout_sec)
    while True:
        pending = []
        for node, stores in req.items():
            live = fetch_storage_for_node(manager, node)
            for sid in sorted(stores):
                if not storage_available(live.get(sid)):
                    pending.append(f'{node}:{sid}')
        if not pending:
            if job:
                _job_log(job, 'storage live — starting sequence')
            return True, None
        if time.time() >= deadline:
            return False, 'storage not live: ' + ', '.join(sorted(pending))
        if job:
            _job_log(job, f'waiting for storage to come live: {", ".join(sorted(pending))}')
        time.sleep(poll)


def _start_guest(manager, step, settings, poll, ha_states):
    """Spec 8: ordered start with local/remote branch (8.1/8.2)."""
    node, vtype, vmid = step['node'], step['type'], step['vmid']
    if not node:
        return 'failed', f'vmid {vmid} has no node in inventory'
    branch = 'remote' if step['placement'] == 'remote' else 'local'
    # 1/1.1 host availability + maintenance (loop)
    ok, err = _wait_node_online(manager, node, ha_states,
                                settings['host_wait_sec'], poll, settings['ignore_maintenance'])
    if not ok:
        return 'failed', err
    # 2/6/7 storage gate. For local storage the home node is the only option;
    # for remote/shared storage we still verify it is active on this node.
    state, err = _storage_gate(manager, step, settings, poll)
    if state == 'skip':
        return 'skipped', err
    if state == 'fail':
        return 'failed', err
    # 8.1 local / 8.2 remote — issue the power verb on the assigned node.
    ok, err = _power(manager, node, vtype, vmid, 'start')
    if not ok:
        return 'failed', f'start failed [{branch}]: {err}'
    timeout = int(step['health'].get('timeout_sec') or settings['step_timeout_sec'])
    ok, err = _wait_health(manager, node, vtype, vmid, step['health'], timeout, poll)
    if not ok:
        return 'failed', err
    return 'done', f'running + healthy [{branch}]'


def _stop_guest(manager, step, settings, poll, ha_states):
    """Spec 9: ordered stop (reverse) with local/remote branch (9.1/9.2)."""
    node, vtype, vmid = step['node'], step['type'], step['vmid']
    if not node:
        return 'failed', f'vmid {vmid} has no node in inventory'
    branch = 'remote' if step['placement'] == 'remote' else 'local'
    # Spec 1: the node must be reachable to issue the stop. Maintenance does NOT
    # block a stop (powering down during maintenance is legitimate).
    ok, err = _wait_node_online(manager, node, ha_states,
                                settings['host_wait_sec'], poll, ignore_maintenance=True)
    if not ok:
        return 'failed', err
    verb = step['stop_mode']  # 'shutdown' | 'stop'
    ok, err = _power(manager, node, vtype, vmid, verb)
    if not ok:
        return 'failed', f'{verb} failed [{branch}]: {err}'
    timeout = int(step['health'].get('timeout_sec') or settings['step_timeout_sec'])
    ok, err = _wait_stopped(manager, node, vtype, vmid, timeout, poll)
    if not ok:
        return 'failed', err
    return 'done', f'stopped [{branch}]'


def _run_step(job, manager, step, rec, settings, poll, ha_states, dry, action):
    """Run a single member (start/stop) and set its rec state. Never raises.

    Records the wall-clock spent on the step (``elapsed_sec``/``elapsed_min``) so
    the UI can show real durations next to the estimate.
    """
    vmid, node, vtype = step['vmid'], step['node'], step['type']
    t0 = time.time()
    rec['started'] = _now_iso()
    try:
        if not step['present']:
            rec['state'] = 'skipped'; rec['detail'] = 'not found in cluster'
            _job_log(job, f"{vmid}: skipped (missing)"); return
        if step['noop']:
            rec['state'] = 'skipped'; rec['detail'] = f"already {step['current_status']}"
            _job_log(job, f"{vmid}: skipped (already {step['current_status']})"); return
        if dry:
            rec['state'] = 'simulated'
            verb = 'start' if action == 'start' else step['stop_mode']
            par = ' [paralelo]' if step.get('parallel') else ''
            eta = (step.get('timing') or {}).get('est_min')
            eta_txt = f", ~{eta} min" if eta else ''
            rec['detail'] = (f"fase {step.get('wave')}{par}: would {verb} {vtype}/{vmid} "
                             f"on {node} [{step['placement']}], then wait "
                             f"({step['health'].get('mode', 'status')}{eta_txt})")
            _job_log(job, f"{vmid}: {rec['detail']}"); return
        # Live re-check: a guest may have changed state since the plan was built.
        live_status = fetch_vm_status(manager, node, vtype, vmid).get('status', 'unknown') if node else 'unknown'
        if action == 'start' and live_status == 'running':
            rec['state'] = 'skipped'; rec['detail'] = 'already running (live)'
            _job_log(job, f"{vmid}: skipped (already running)"); return
        if action == 'stop' and live_status == 'stopped':
            rec['state'] = 'skipped'; rec['detail'] = 'already stopped (live)'
            _job_log(job, f"{vmid}: skipped (already stopped)"); return
        try:
            runner = _start_guest if action == 'start' else _stop_guest
            st, detail = runner(manager, step, settings, poll, ha_states)
            rec['state'] = st; rec['detail'] = detail
            _job_log(job, f"{vmid}: {st} ({detail})")
        except Exception as e:
            rec['state'] = 'failed'; rec['detail'] = str(e)
            _job_log(job, f"{vmid}: FAILED {e}")
    finally:
        rec['elapsed_sec'] = round(time.time() - t0, 1)
        rec['elapsed_min'] = _minutes(rec['elapsed_sec'])


def _execute_job(job, manager, group, inventory, steps):
    """Execute the plan as sequential phases (waves). Members sharing a phase
    (same `order`) run in PARALLEL; the next phase starts only after the current
    one finished (each start health-gated). Stop walks the phases in reverse."""
    settings = _group_settings(group)
    poll = settings['poll_interval_sec']
    dry = job['dry_run']
    action = job['action']
    ha_states = {} if dry else fetch_ha_node_states(manager)

    # Pre-create every rec (ordered) so the UI shows the full plan immediately.
    recs = []
    with _jobs_lock:
        for step in steps:
            rec = dict(step, state='pending', detail=None, ts=_now_iso())
            job['steps'].append(rec)
            recs.append(rec)

    # Spec 6/7: hold the WHOLE sequence until the backing storage is live (e.g.
    # after a power event the shared array is still coming up). Best-effort: on
    # timeout we log and fall through to the per-member gate, which then applies
    # each member's storage_policy. Skipped for dry-run.
    if action == 'start' and not dry:
        ready_sec = int(settings.get('storage_ready_sec', 0) or 0)
        if ready_sec > 0:
            ok_s, err_s = _wait_storage_ready(manager, steps, ready_sec, poll, job)
            if not ok_s:
                _job_log(job, f'storage not live after {ready_sec}s ({err_s}); '
                              'proceeding — per-member storage policy will decide')

    # Group consecutive same-wave steps into parallel batches.
    waves = []
    for step, rec in zip(steps, recs):
        if waves and waves[-1][0] == step.get('wave'):
            waves[-1][1].append((step, rec))
        else:
            waves.append((step.get('wave'), [(step, rec)]))

    failed = False
    for _wave_no, items in waves:
        if len(items) == 1:
            s, r = items[0]
            _run_step(job, manager, s, r, settings, poll, ha_states, dry, action)
        else:
            # parallel phase
            with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
                futs = [ex.submit(_run_step, job, manager, s, r, settings, poll,
                                  ha_states, dry, action) for s, r in items]
                for f in futs:
                    f.result()  # _run_step swallows its own errors
        if any(r['state'] == 'failed' for _, r in items):
            failed = True
            if not settings['continue_on_error']:
                break  # don't launch later phases

    job['status'] = 'failed' if failed else 'done'
    job['finished'] = _now_iso()
    job['elapsed_sec'] = round(time.time() - job.get('started_epoch', time.time()), 1)
    job['elapsed_min'] = _minutes(job['elapsed_sec'])


def _dispatch_group(manager, cluster_id, group, action, dry_run, username, sync=False):
    """Build a plan for ``group`` and run it as a job (shared by the HTTP execute
    handler and the autostart runner). With ``sync`` the job runs in the calling
    thread (the autostart runner is already a background thread); otherwise it
    runs in its own daemon thread. Returns (job, steps). May raise ValueError on
    an unorderable group (cycle / unknown dependency)."""
    inv = fetch_inventory(manager)
    vm_configs, storage_by_node, storage_defs = _collect_plan_inputs(manager, group, inv)
    steps = build_plan(group, inv, vm_configs, storage_by_node, action, storage_defs)
    job = _new_job(cluster_id, group['id'], action, dry_run, username)
    log_audit(user=username,
              action=f'power.{action}' + ('_dryrun' if dry_run else ''),
              details=f"group={group['id']} steps={len(steps)} dry_run={dry_run} by={username}",
              cluster=cluster_id)
    if sync:
        _execute_job(job, manager, group, inv, steps)
    else:
        threading.Thread(target=_execute_job, args=(job, manager, group, inv, steps),
                         daemon=True, name=f'power-{job["id"]}').start()
    return job, steps


# ---------------------------------------------------------------------------
# Auto-update (in-plugin)
# ---------------------------------------------------------------------------
# The plugin can check a raw source (default: this repo on GitHub) for a newer
# version and apply it live — PegaProx's /reload re-imports the module from
# disk, so no service restart is needed. The host-side maintenance timer
# (installed by install.sh) does the same unattended AND restores the plugin if
# a PegaProx upgrade ever wipes the plugins dir (persistence).

UPDATE_FILES = ('__init__.py', 'manifest.json', 'power.html')

_DEFAULT_UPDATE_SETTINGS = {
    'source': 'https://raw.githubusercontent.com/alfonsokuen/pegaprox-plugin-proxmox-power/main',
    # Fallback mirrors tried in order when the primary source is unreachable
    # (e.g. a host whose DNS can't resolve raw.githubusercontent.com). jsDelivr
    # serves the same public repo over a different CDN/DNS path, so it commonly
    # succeeds where the GitHub raw host is blocked or unresolvable.
    'mirrors': [
        'https://cdn.jsdelivr.net/gh/alfonsokuen/pegaprox-plugin-proxmox-power@main',
        'https://fastly.jsdelivr.net/gh/alfonsokuen/pegaprox-plugin-proxmox-power@main',
    ],
    'auto_apply': False,
    'check_interval_hours': 24,
}


def _update_settings():
    cfg = _load_config()
    s = dict(_DEFAULT_UPDATE_SETTINGS)
    s.update(cfg.get('updates') or {})
    return s


def version_tuple(v):
    """Lenient semver -> tuple of ints ('1.2.3' -> (1,2,3)). Non-numeric -> 0."""
    out = []
    for part in str(v if v is not None else '0').split('.'):
        digits = ''.join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def version_gt(a, b):
    """True if version a is strictly greater than b."""
    ta, tb = version_tuple(a), version_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return ta > tb


def _local_version():
    try:
        with open(os.path.join(PLUGIN_DIR, 'manifest.json')) as f:
            return json.load(f).get('version', '0')
    except Exception:
        return '0'


def _fetch_remote_text(source, name, timeout=10):
    """Fetch <source>/<name> as text. Lazy-imports requests (PegaProx ships it)."""
    import requests
    url = f"{source.rstrip('/')}/{name}"
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def _update_sources(explicit=None):
    """Ordered, de-duped list of mirror base URLs to try: the explicit/configured
    source first, then the fallback mirrors. A single unreachable host (blocked
    DNS, dead CDN) then never blocks updates on its own."""
    s = _update_settings()
    primary = explicit or s.get('source') or _DEFAULT_UPDATE_SETTINGS['source']
    mirrors = s.get('mirrors')
    if not isinstance(mirrors, list):
        mirrors = _DEFAULT_UPDATE_SETTINGS['mirrors']
    out, seen = [], set()
    for u in [primary, *mirrors]:
        u = (u or '').rstrip('/')
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def check_update(source=None):
    """Compare local manifest version against the remote source. Never raises.
    Tries every mirror in order and reports the one that answered."""
    sources = _update_sources(source)
    cur = _local_version()
    errors = []
    for src in sources:
        try:
            remote = json.loads(_fetch_remote_text(src, 'manifest.json'))
            latest = remote.get('version', '0')
            return {'current': cur, 'latest': latest,
                    'update_available': version_gt(latest, cur), 'source': src}
        except Exception as e:
            errors.append(f'{src}: {str(e)[:120]}')
    return {'current': cur, 'latest': None, 'update_available': False,
            'source': sources[0] if sources else None,
            'error': ' | '.join(errors)[:300] or 'no update sources'}


def apply_update(source=None, allow_downgrade=False):
    """Download + validate + atomically install the runtime files.

    Validation is fail-closed: the new manifest must parse, the new __init__.py
    must byte-compile, and power.html must be non-empty — otherwise nothing is
    written. A strictly-older remote version is refused unless ``allow_downgrade``
    (re-applying the same version is allowed, for repair). Each replaced file is
    backed up to <file>.bak.
    """
    # Download the WHOLE file set from a single reachable mirror (never mix files
    # across mirrors). Try each in order until one serves them all.
    sources = _update_sources(source)
    downloaded, used, errors = None, None, []
    for src in sources:
        try:
            downloaded = {name: _fetch_remote_text(src, name) for name in UPDATE_FILES}
            used = src
            break
        except Exception as e:
            errors.append(f'{src}: {str(e)[:120]}')
    if downloaded is None:
        raise RuntimeError('no update source reachable — ' + ' | '.join(errors))

    new_manifest = json.loads(downloaded['manifest.json'])
    new_ver = new_manifest.get('version', '0')
    cur = _local_version()
    if not allow_downgrade and version_gt(cur, new_ver):
        raise RuntimeError(
            f'refusing downgrade {cur} -> {new_ver} (pass allow_downgrade to force)')
    if not downloaded['power.html'].strip():
        raise RuntimeError('downloaded power.html is empty')

    import tempfile
    import py_compile
    tmp_py = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False,
                                         encoding='utf-8') as tf:
            tf.write(downloaded['__init__.py'])
            tmp_py = tf.name
        py_compile.compile(tmp_py, doraise=True)
    finally:
        if tmp_py and os.path.exists(tmp_py):
            os.unlink(tmp_py)

    for name, content in downloaded.items():
        path = os.path.join(PLUGIN_DIR, name)
        try:
            if os.path.exists(path):
                shutil.copy2(path, path + '.bak')
        except Exception:
            pass
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp, path)
    return {'applied': True, 'from': cur, 'to': new_ver, 'source': used}


# ---------------------------------------------------------------------------
# Autostart on PegaProx ready (opt-in unattended boot of the whole cluster)
# ---------------------------------------------------------------------------
# When PegaProx comes up after a power event, optionally bring up the operator's
# configured group(s) automatically — ordered, phased and health-gated, exactly
# like a manual real run. It is OFF by default ("depende del usuario") and fires
# at most ONCE per boot: a marker keyed on the OS boot id survives plugin
# reloads/updates so a reload never re-triggers a mass power-on. It only ever
# *starts* (never auto-stops), waits for the cluster manager to connect first,
# and skips guests that are already running (idempotent).

_DEFAULT_AUTOSTART = {
    'enabled': False,        # opt-in — never auto power-on unless the user turns it on
    'cluster_id': '',        # fallback cluster when a group has none
    'groups': [],            # ordered list of group ids to start, in order
    'delay_sec': 30,         # grace after PegaProx is up before firing
    'wait_cluster_sec': 300, # max wait for the cluster manager to connect
    'storage_ready_sec': 600,# wait for shared storage to come live before starting (spec 6/7)
    'stop_on_error': False,  # stop launching later groups after a failed one
}

_autostart_started = False
_autostart_lock = threading.Lock()


def _autostart_settings():
    cfg = _load_config()
    s = dict(_DEFAULT_AUTOSTART)
    s.update(cfg.get('autostart') or {})
    return s


def _autostart_state_path():
    return os.path.join(PLUGIN_DIR, '.autostart_state.json')


def _read_autostart_state():
    try:
        with open(_autostart_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_autostart_state(state):
    try:
        tmp = _autostart_state_path() + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _autostart_state_path())
    except Exception as e:
        log.warning(f'[{PLUGIN_ID}] autostart state write failed: {e}')


def _current_boot_id():
    """A token that is stable within one OS boot and changes on reboot.

    Used to fire autostart only once per boot while ignoring plugin reloads.
    Linux: /proc/sys/kernel/random/boot_id, else btime from /proc/stat. Returns
    None when it can't be determined (e.g. on a dev box); callers then fall back
    to a per-process guard.
    """
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except Exception:
        pass
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('btime'):
                    return 'btime-' + line.split()[1]
    except Exception:
        pass
    return None


def _wait_manager(cluster_id, timeout_sec):
    """Block until the cluster manager is connected, or timeout. Returns the
    manager or None."""
    deadline = time.time() + max(0, timeout_sec)
    while True:
        manager = None
        try:
            manager, err = get_connected_manager(cluster_id)
            if err:
                manager = None
        except Exception:
            manager = None
        if manager and getattr(manager, 'is_connected', True):
            return manager
        if time.time() >= deadline:
            return None
        time.sleep(5)


def _run_autostart_groups(settings, username='autostart', dry_run=False, sync=True):
    """Start every configured group in order. Returns a list of per-group
    result dicts. Each group resolves its own cluster (or the autostart
    fallback) and waits for the manager to connect before running."""
    results = []
    for gid in (settings.get('groups') or []):
        cfg = _load_config()
        group = _get_group(cfg, gid)
        if not group:
            results.append({'group': gid, 'status': 'skipped', 'detail': 'group not found'})
            continue
        cluster_id = group.get('cluster_id') or settings.get('cluster_id')
        if not cluster_id:
            results.append({'group': gid, 'status': 'skipped', 'detail': 'no cluster_id'})
            continue
        manager = _wait_manager(cluster_id, int(settings.get('wait_cluster_sec', 300) or 0))
        if not manager:
            results.append({'group': gid, 'status': 'failed', 'detail': 'cluster manager not connected'})
            if settings.get('stop_on_error'):
                break
            continue
        # Hold the sequence until shared storage is live (spec 6/7). Apply the
        # autostart-wide wait unless the group already asks for a longer one.
        ready_sec = int(settings.get('storage_ready_sec', 0) or 0)
        if ready_sec:
            group = dict(group)
            gs = dict(group.get('settings') or {})
            gs['storage_ready_sec'] = max(int(gs.get('storage_ready_sec', 0) or 0), ready_sec)
            group['settings'] = gs
        try:
            job, steps = _dispatch_group(manager, cluster_id, group, 'start', dry_run, username, sync=sync)
            results.append({'group': gid, 'job': job['id'], 'steps': len(steps),
                            'status': job['status'] if sync else 'running',
                            'elapsed_min': job.get('elapsed_min')})
            if sync and job['status'] == 'failed' and settings.get('stop_on_error'):
                break
        except Exception as e:
            results.append({'group': gid, 'status': 'failed', 'detail': str(e)[:200]})
            if settings.get('stop_on_error'):
                break
    return results


def _autostart_runner():
    """Background thread started from register(): fire the configured groups once
    per boot. Never raises (this runs detached)."""
    try:
        settings = _autostart_settings()
        if not settings.get('enabled'):
            return
        boot_id = _current_boot_id()
        with _autostart_lock:
            state = _read_autostart_state()
            # Already fired (or in progress) for this boot -> skip on reload.
            if boot_id and state.get('boot_id') == boot_id and (
                    state.get('completed') or state.get('started')):
                log.info(f'[{PLUGIN_ID}] autostart: already handled this boot, skipping')
                return
            _write_autostart_state({'boot_id': boot_id, 'started': _now_iso(),
                                    'completed': False})
        delay = int(settings.get('delay_sec', 30) or 0)
        if delay:
            time.sleep(delay)
        log.info(f'[{PLUGIN_ID}] autostart: firing groups {settings.get("groups")}')
        results = _run_autostart_groups(settings, username='autostart', dry_run=False, sync=True)
        state = _read_autostart_state()
        state.update({'completed': True, 'finished': _now_iso(), 'results': results})
        _write_autostart_state(state)
        log.info(f'[{PLUGIN_ID}] autostart: done {results}')
    except Exception as e:
        log.warning(f'[{PLUGIN_ID}] autostart runner error: {e}')


def _maybe_schedule_autostart():
    """Called from register(): spawn the autostart thread when enabled. The
    once-per-boot marker guarantees a reload won't re-trigger a power-on."""
    global _autostart_started
    try:
        if _autostart_settings().get('enabled') and not _autostart_started:
            _autostart_started = True
            threading.Thread(target=_autostart_runner, daemon=True,
                             name='power-autostart').start()
            log.info(f'[{PLUGIN_ID}] autostart scheduled (enabled)')
    except Exception as e:
        log.warning(f'[{PLUGIN_ID}] autostart schedule error: {e}')


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
    # Defense in depth: the catch-all already enforces plugins.view, but the UI
    # is only useful to users who can read VM state, so gate it on vm.view too.
    if (err := _require(PERM_VIEW)):
        return err
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
        # Friendly name lives on the manager's config object (manager.config.name);
        # the manager itself only carries .id. Fall back to the id.
        cfg = getattr(mgr, 'config', None)
        out.append({
            'id': cid,
            'name': getattr(cfg, 'name', None) or cid,
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
    cfg = _load_config()
    # Self-heal stale autostart references on read so a dirty config (left behind
    # by an older build that didn't reconcile on group delete) never surfaces.
    if _reconcile_autostart_groups(cfg):
        _save_config(cfg)
    return jsonify(cfg)


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
        seen_vmid = set()
        for m in g.get('members', []):
            if 'vmid' not in m:
                return jsonify({'error': f"group '{g.get('id')}': a member is missing 'vmid'"}), 400
            try:
                vmid = int(m['vmid'])
            except (TypeError, ValueError):
                return jsonify({'error': f"group '{g.get('id')}': vmid '{m.get('vmid')}' is not an integer"}), 400
            if vmid in seen_vmid:
                return jsonify({'error': f"group '{g.get('id')}': duplicate vmid {vmid}"}), 400
            seen_vmid.add(vmid)
            order = m.get('order')
            sub = m.get('suborder', 0)
            # Same order across members is VALID — it means they boot in parallel
            # (one phase/wave). Only reject impossible values.
            if isinstance(order, (int, float)) and order < 1:
                return jsonify({'error': f"group '{g.get('id')}': order for vmid {vmid} must be >= 1"}), 400
            if isinstance(sub, (int, float)) and sub < 0:
                return jsonify({'error': f"group '{g.get('id')}': suborder for vmid {vmid} must be >= 0"}), 400
        try:
            topo_order(g.get('members', []))
        except ValueError as e:
            return jsonify({'error': f"group '{g.get('id')}': {e}"}), 400
    cfg = _load_config()
    cfg['groups'] = groups
    # Deleting/renaming a group must not leave an orphaned reference behind in
    # autostart.groups (the root cause of dangling refs). Reconcile in the same
    # write so a removed group can never linger as a silent no-op at boot.
    pruned = _reconcile_autostart_groups(cfg)
    _save_config(cfg)
    log_audit(user=_username(), action='power.config_saved',
              details=f'{len(groups)} group(s)' + (f' autostart_pruned={pruned}' if pruned else ''))
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
    """Gather configs + per-node storage + cluster storage defs for a plan."""
    vm_configs, nodes_needed = {}, set()
    for m in group.get('members', []):
        vmid = int(m['vmid'])
        if vmid in inv:
            n = inv[vmid]
            nodes_needed.add(n['node'])
            vm_configs[vmid] = fetch_vm_config(manager, n['node'], n['type'], vmid)
    storage_by_node = {n: fetch_storage_for_node(manager, n) for n in nodes_needed}
    storage_defs = fetch_cluster_storage_defs(manager)
    return vm_configs, storage_by_node, storage_defs


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
        vm_configs, storage_by_node, storage_defs = _collect_plan_inputs(manager, group, inv)
        steps = build_plan(group, inv, vm_configs, storage_by_node, action, storage_defs)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': safe_error(e, 'plan failed')}), 500
    return jsonify({'cluster_id': cluster_id, 'group': group['id'],
                    'action': action, 'steps': steps, 'timing': plan_timing(steps)})


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
        job, steps = _dispatch_group(manager, cluster_id, group, action, dry_run, _username())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': safe_error(e, 'plan failed')}), 500
    return jsonify({'job_id': job['id'], 'dry_run': dry_run, 'steps': len(steps),
                    'timing': plan_timing(steps)})


def update_check_handler():
    if (err := _require(PERM_VIEW)):
        return err
    source = (request.args.get('source')
              or (request.get_json(silent=True) or {}).get('source') or '').strip() or None
    return jsonify(check_update(source))


def update_apply_handler():
    if (err := _require(PERM_POWER)):
        return err
    body = request.get_json(silent=True) or {}
    source = (body.get('source') or '').strip() or None
    allow_downgrade = body.get('allow_downgrade') is True
    try:
        result = apply_update(source, allow_downgrade=allow_downgrade)
    except Exception as e:
        # Apply errors are operator-facing config/network issues (downgrade
        # refused, empty html, broken py, unreachable source) — surface them.
        return jsonify({'error': str(e)[:300]}), 502
    log_audit(user=_username(), action='power.plugin_updated',
              details=f"{result['from']} -> {result['to']}")
    # Hint the UI to live-reload the plugin via PegaProx's reload route.
    result['reload_url'] = f'/api/plugins/{PLUGIN_ID}/reload'
    return jsonify(result)


def autostart_config_handler():
    if (err := _require(PERM_VIEW)):
        return err
    cfg = _load_config()
    if _reconcile_autostart_groups(cfg):
        _save_config(cfg)
    settings = dict(_DEFAULT_AUTOSTART)
    settings.update(cfg.get('autostart') or {})
    return jsonify({'settings': settings, 'state': _read_autostart_state()})


def autostart_save_handler():
    if (err := _require(PERM_POWER)):
        return err
    body = request.get_json(silent=True) or {}
    a = body.get('autostart') if isinstance(body.get('autostart'), dict) else body
    out = dict(_DEFAULT_AUTOSTART)
    out['enabled'] = bool(a.get('enabled'))
    out['cluster_id'] = str(a.get('cluster_id') or '').strip()
    groups = a.get('groups') or []
    if not isinstance(groups, list):
        return jsonify({'error': 'autostart.groups must be a list'}), 400
    out['groups'] = [str(g) for g in groups]
    for key, lo in (('delay_sec', 0), ('wait_cluster_sec', 0), ('storage_ready_sec', 0)):
        try:
            out[key] = max(lo, int(a.get(key, _DEFAULT_AUTOSTART[key])))
        except (TypeError, ValueError):
            return jsonify({'error': f'autostart.{key} must be an integer'}), 400
    out['stop_on_error'] = bool(a.get('stop_on_error'))
    # Reconcile referenced groups against the ones that actually exist. A
    # reference to a deleted/renamed group can only ever be a silent no-op at
    # boot ("group not found"), so it must never linger in the JSON.
    cfg = _load_config()
    known = {g.get('id') for g in cfg.get('groups', [])}
    missing = [g for g in out['groups'] if g not in known]
    if out['enabled'] and missing:
        # Enabling with dangling references would silently no-op at boot — reject
        # so the operator notices and fixes the selection.
        return jsonify({'error': f"unknown group(s): {', '.join(missing)}"}), 400
    # Disabled save (or no dangling refs): prune the dead references instead of
    # persisting them. This is what stops orphaned group ids/names from sticking
    # in the config after a group is deleted or recreated with a new id.
    pruned = []
    if missing:
        pruned = missing
        out['groups'] = [g for g in out['groups'] if g in known]
    cfg['autostart'] = out
    _save_config(cfg)
    log_audit(user=_username(), action='power.autostart_saved',
              details=f"enabled={out['enabled']} groups={out['groups']}"
                      + (f" pruned={pruned}" if pruned else ''))
    resp = {'ok': True, 'autostart': out}
    if pruned:
        resp['pruned'] = pruned
    return jsonify(resp)


def autostart_run_handler():
    """Manually trigger the autostart groups now (defaults to dry-run). Lets the
    operator preview/test the unattended boot without rebooting."""
    if (err := _require(PERM_POWER)):
        return err
    body = request.get_json(silent=True) or {}
    dry_run = not (body.get('confirm') is True)
    settings = _autostart_settings()
    if not settings.get('groups'):
        return jsonify({'error': 'no autostart groups configured'}), 400
    # Run async so the HTTP call returns immediately; results land as normal jobs.
    results = _run_autostart_groups(settings, username=_username(), dry_run=dry_run, sync=False)
    log_audit(user=_username(), action='power.autostart_run' + ('_dryrun' if dry_run else ''),
              details=f"groups={settings.get('groups')} dry_run={dry_run}")
    return jsonify({'ok': True, 'dry_run': dry_run, 'results': results})


def job_handler():
    if (err := _require(PERM_VIEW)):
        return err
    job_id = request.args.get('id', '').strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
        # Snapshot under the lock: the executor thread mutates steps/log live, so
        # serialize a shallow copy (incl. a copied steps list) to avoid a torn read.
        snap = dict(job, steps=list(job['steps']), log=list(job['log'])) if job else None
    if not snap:
        return jsonify({'error': 'job not found'}), 404
    return jsonify(snap)


def jobs_handler():
    if (err := _require(PERM_VIEW)):
        return err
    with _jobs_lock:
        items = sorted(_jobs.values(), key=lambda j: j['started'], reverse=True)
        summary = [{k: j.get(k) for k in
                    ('id', 'cluster_id', 'group', 'action', 'dry_run',
                     'status', 'started', 'finished', 'user',
                     'elapsed_sec', 'elapsed_min')} for j in items]
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
        'update/check': update_check_handler,
        'update/apply': update_apply_handler,
        'autostart/config': autostart_config_handler,
        'autostart/save': autostart_save_handler,
        'autostart/run': autostart_run_handler,
    }
    for path, handler in routes.items():
        register_plugin_route(PLUGIN_ID, path, handler)
    log.info(f"[{PLUGIN_ID}] Registered {len(routes)} routes")
    # Opt-in unattended boot: fires once per OS boot when the operator enabled it.
    _maybe_schedule_autostart()
