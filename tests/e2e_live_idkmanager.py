# -*- coding: utf-8 -*-
"""E2E (read-only) against REAL IDKMANAGER production data.

Inventory, raw VM configs and per-node storage below were captured live from the
IDKMANAGER Proxmox cluster (pve1/pve2/pve3) via the PegaProx MCP + pvesh on
2026-06-07. We feed them through the *real* engine functions and assert the
ordered plan + storage classification. No Proxmox mutation occurs — this only
exercises planning/classification on production-shaped data.

Run explicitly:  python tests/e2e_live_idkmanager.py
"""

import os
import sys

try:  # Windows consoles default to cp1252; force UTF-8 for the ✓/↳ glyphs.
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# Stub host modules then import the plugin as a module (mirrors conftest).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import conftest  # noqa: E402  (installs fakes at import)
conftest._install_fakes()
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'proxmox_power',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '__init__.py'))
P = importlib.util.module_from_spec(spec)
sys.modules['proxmox_power'] = P
spec.loader.exec_module(P)

# --- REAL captured data -----------------------------------------------------

INVENTORY = {
    110: {'node': 'pve3', 'name': 'IDKMANAGER-IA-SERVER-01', 'type': 'qemu', 'status': 'running'},
    120: {'node': 'pve1', 'name': 'IDKMANAGER-IA-SERVER-02', 'type': 'qemu', 'status': 'running'},
    114: {'node': 'pve2', 'name': 'IDKMANAGER-Blue-Iris',    'type': 'qemu', 'status': 'stopped'},
}

# Real Proxmox raw configs (the `raw` field returned by the API).
VM_CONFIGS = {
    110: {'scsi0': 'TN-NVMeOF-VM:vm-110-disk-0.qcow2,discard=on,iothread=1,size=300G,ssd=1',
          'efidisk0': 'TN-NVMeOF-VM:vm-110-disk-1.qcow2,efitype=4m,size=528K',
          'onboot': 1, 'boot': 'order=scsi0'},
    120: {'scsi0': 'TN-NVMeOF-VM:vm-120-disk-1.qcow2,size=200G',
          'ide2': 'TN-NVMeOF-VM:vm-120-cloudinit,media=cdrom',
          'unused0': 'TN-NVMeOF-VM:vm-120-disk-0.qcow2',
          'onboot': 0, 'boot': 'order=scsi0'},
    114: {'scsi0': 'TN-NVMeOF-VM:vm-114-disk-1.qcow2,discard=on,iothread=1,size=60G,ssd=1',
          'efidisk0': 'TN-NVMeOF-VM:vm-114-disk-0.qcow2,efitype=4m,size=528K',
          'onboot': 0, 'boot': 'order=scsi0'},
}

# Real per-node storage status (subset of fields the engine reads).
STORAGE_BY_NODE = {
    'pve1': {
        'TN-NVMeOF-VM': {'type': 'lvm', 'shared': 1, 'enabled': 1, 'active': 1},
        'local': {'type': 'dir', 'shared': 0, 'enabled': 1, 'active': 1},
        'local-zfs': {'type': 'zfspool', 'shared': 0, 'enabled': 1, 'active': 1},
        'PB_NFS': {'type': 'nfs', 'shared': 1, 'enabled': 1, 'active': 1},
    },
    'pve2': {
        'TN-NVMeOF-VM': {'type': 'lvm', 'shared': 1, 'enabled': 1, 'active': 1},
        'local': {'type': 'dir', 'shared': 0, 'enabled': 1, 'active': 1},
    },
    'pve3': {
        'TN-NVMeOF-VM': {'type': 'lvm', 'shared': 1, 'enabled': 1, 'active': 1},
        'local': {'type': 'dir', 'shared': 0, 'enabled': 1, 'active': 1},
    },
}

GROUP = {
    'id': 'idk-ia-servers',
    'name': 'IDKMANAGER IA Servers',
    'settings': {'stop_mode': 'shutdown'},
    'members': [
        {'vmid': 110, 'name': 'ia-01', 'order': 10, 'health': {'mode': 'agent'}},
        {'vmid': 120, 'name': 'ia-02', 'order': 20, 'depends_on': [110], 'health': {'mode': 'agent'}},
        {'vmid': 114, 'name': 'blue-iris', 'order': 30, 'depends_on': [120], 'health': {'mode': 'status'}},
    ],
}


def _print_plan(title, steps):
    print(f"\n  {title}")
    for i, s in enumerate(steps, 1):
        dep = ('↳ ' + ','.join(map(str, s['depends_on']))) if s['depends_on'] else '—'
        print(f"    {i}. #{s['vmid']:<4} {s['name']:<10} {s['type']}/{s['node']:<4} "
              f"[{s['placement']:<6}] storage={s['storage_state']:<11} "
              f"dep={dep:<8} cur={s['current_status']} "
              f"{'(no-op)' if s['noop'] else ''}")


def main():
    print("E2E read-only — IDKMANAGER production data (no mutation)")

    # --- storage classification on the REAL NVMe-oF-over-LVM shared LUN ------
    nvme = STORAGE_BY_NODE['pve1']['TN-NVMeOF-VM']
    assert P.classify_storage(nvme) == 'remote', "shared NVMe-oF LVM must be remote"
    assert P.classify_storage(STORAGE_BY_NODE['pve1']['local-zfs']) == 'local'
    print("  ✓ TN-NVMeOF-VM (lvm, shared=1) classified remote; local-zfs classified local")

    # --- START plan ---------------------------------------------------------
    start = P.build_plan(GROUP, INVENTORY, VM_CONFIGS, STORAGE_BY_NODE, 'start')
    assert [s['vmid'] for s in start] == [110, 120, 114], "start order must follow depends_on"
    assert all(s['placement'] == 'remote' for s in start), "all IA disks on shared NVMe-oF"
    assert all(s['storage_state'] == 'ok' for s in start), "all storages active"
    assert start[0]['noop'] and start[1]['noop'], "110/120 already running -> no-op"
    assert not start[2]['noop'], "114 stopped -> real start"
    # cdrom (vm-120-cloudinit, media=cdrom) must not appear as a backing storage gate
    s120 = next(s for s in start if s['vmid'] == 120)
    assert {x['storage'] for x in s120['storage']} == {'TN-NVMeOF-VM'}
    _print_plan("START plan (dependency order):", start)

    # --- STOP plan (reverse) ------------------------------------------------
    stop = P.build_plan(GROUP, INVENTORY, VM_CONFIGS, STORAGE_BY_NODE, 'stop')
    assert [s['vmid'] for s in stop] == [114, 120, 110], "stop must reverse the start order"
    assert stop[0]['stop_mode'] == 'shutdown'
    _print_plan("STOP plan (reverse order):", stop)

    # --- boot settings surfaced from real config ----------------------------
    assert P.parse_startup(VM_CONFIGS[110]) == {'order': None, 'up': None, 'down': None}
    assert P.extract_vm_storages(VM_CONFIGS[110]) == {'TN-NVMeOF-VM'}
    print("\n  ✓ boot/storage settings parsed from real configs")

    print("\nE2E PASSED ✅  (3 guests, NVMe-oF shared storage, cross-node dependency chain)")


if __name__ == '__main__':
    main()
