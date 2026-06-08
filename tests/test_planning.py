# -*- coding: utf-8 -*-
"""Unit tests for the pure planning/validation logic (no PegaProx, no Proxmox)."""

import pytest


# --- storage classification -------------------------------------------------

def test_classify_storage_shared_flag_wins(plugin):
    # An LVM (normally local) marked shared (NVMe-oF/shared-LUN) -> remote.
    assert plugin.classify_storage({'type': 'lvm', 'shared': 1}) == 'remote'


def test_classify_storage_by_type(plugin):
    assert plugin.classify_storage({'type': 'nfs'}) == 'remote'
    assert plugin.classify_storage({'type': 'iscsi'}) == 'remote'
    assert plugin.classify_storage({'type': 'cifs'}) == 'remote'
    assert plugin.classify_storage({'type': 'dir'}) == 'local'
    assert plugin.classify_storage({'type': 'lvmthin'}) == 'local'


def test_storage_available(plugin):
    assert plugin.storage_available({'enabled': 1, 'active': 1}) is True
    assert plugin.storage_available({'enabled': 1, 'active': 0}) is False
    assert plugin.storage_available({'enabled': 0, 'active': 1}) is False
    assert plugin.storage_available(None) is False
    # enabled defaults to 1 when the key is absent (Proxmox omits it when on)
    assert plugin.storage_available({'active': 1}) is True


# --- config disk extraction -------------------------------------------------

def test_extract_vm_storages_qemu(plugin):
    cfg = {
        'scsi0': 'local-lvm:vm-100-disk-0,size=32G',
        'virtio0': 'nvme-shared:vm-100-disk-1,size=100G',
        'ide2': 'none,media=cdrom',
        'net0': 'virtio=AA:BB,bridge=vmbr0',
        'efidisk0': 'local-lvm:vm-100-disk-2',
    }
    assert plugin.extract_vm_storages(cfg) == {'local-lvm', 'nvme-shared'}


def test_extract_vm_storages_lxc(plugin):
    cfg = {'rootfs': 'nfs-ct:subvol-201-disk-0,size=8G',
           'mp0': 'local:subvol-201-disk-1,mp=/data'}
    assert plugin.extract_vm_storages(cfg) == {'nfs-ct', 'local'}


def test_extract_vm_storages_skips_cdrom_iso(plugin):
    cfg = {'ide2': 'isos:iso/debian.iso,media=cdrom'}
    assert plugin.extract_vm_storages(cfg) == set()


# --- startup parsing --------------------------------------------------------

def test_parse_startup(plugin):
    assert plugin.parse_startup({'startup': 'order=3,up=30,down=60'}) == {
        'order': 3, 'up': 30, 'down': 60}
    assert plugin.parse_startup({}) == {'order': None, 'up': None, 'down': None}


# --- topological ordering ---------------------------------------------------

def test_topo_order_respects_dependencies(plugin):
    members = [
        {'vmid': 101, 'order': 30, 'depends_on': [100]},
        {'vmid': 100, 'order': 20, 'depends_on': [110]},
        {'vmid': 110, 'order': 10},
    ]
    order = [m['vmid'] for m in plugin.topo_order(members)]
    assert order == [110, 100, 101]


def test_topo_order_suborder_tiebreak(plugin):
    members = [
        {'vmid': 102, 'order': 30, 'suborder': 1, 'depends_on': [100]},
        {'vmid': 101, 'order': 30, 'suborder': 0, 'depends_on': [100]},
        {'vmid': 100, 'order': 20},
    ]
    order = [m['vmid'] for m in plugin.topo_order(members)]
    assert order == [100, 101, 102]


def test_topo_order_detects_cycle(plugin):
    members = [
        {'vmid': 1, 'depends_on': [2]},
        {'vmid': 2, 'depends_on': [1]},
    ]
    with pytest.raises(ValueError, match='cycle'):
        plugin.topo_order(members)


def test_topo_order_unknown_dependency(plugin):
    members = [{'vmid': 1, 'depends_on': [999]}]
    with pytest.raises(ValueError, match='unknown'):
        plugin.topo_order(members)


# --- plan building ----------------------------------------------------------

def _fixture_group(plugin):
    return {
        'id': 'g', 'settings': {'stop_mode': 'shutdown'},
        'members': [
            {'vmid': 110, 'order': 10, 'health': {'mode': 'status'}},
            {'vmid': 100, 'order': 20, 'depends_on': [110],
             'health': {'mode': 'agent'}},
        ],
    }


def _fixture_inventory():
    return {
        110: {'node': 'pve1', 'name': 'gw', 'type': 'qemu', 'status': 'stopped'},
        100: {'node': 'pve2', 'name': 'db', 'type': 'qemu', 'status': 'stopped'},
    }


def test_build_plan_start_order(plugin):
    group = _fixture_group(plugin)
    inv = _fixture_inventory()
    cfgs = {110: {'scsi0': 'local-lvm:vm-110-disk-0'},
            100: {'scsi0': 'nfs-shared:vm-100-disk-0'}}
    storage = {
        'pve1': {'local-lvm': {'type': 'lvmthin', 'enabled': 1, 'active': 1}},
        'pve2': {'nfs-shared': {'type': 'nfs', 'enabled': 1, 'active': 1, 'shared': 1}},
    }
    steps = plugin.build_plan(group, inv, cfgs, storage, 'start')
    assert [s['vmid'] for s in steps] == [110, 100]
    assert steps[0]['placement'] == 'local'
    assert steps[1]['placement'] == 'remote'
    assert steps[1]['storage_state'] == 'ok'


def test_build_plan_stop_is_reversed(plugin):
    group = _fixture_group(plugin)
    inv = _fixture_inventory()
    steps = plugin.build_plan(group, inv, {}, {}, 'stop')
    assert [s['vmid'] for s in steps] == [100, 110]


def test_build_plan_flags_unavailable_storage(plugin):
    group = {'id': 'g', 'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    cfgs = {100: {'scsi0': 'iscsi-lun:vm-100-disk-0'}}
    storage = {'pve1': {'iscsi-lun': {'type': 'iscsi', 'enabled': 1, 'active': 0}}}
    steps = plugin.build_plan(group, inv, cfgs, storage, 'start')
    assert steps[0]['storage_state'] == 'unavailable'
    assert steps[0]['placement'] == 'remote'


def test_build_plan_running_vm_is_noop_on_start(plugin):
    group = {'id': 'g', 'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'running'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    assert steps[0]['noop'] is True


def test_build_plan_missing_member_marked_absent(plugin):
    group = {'id': 'g', 'members': [{'vmid': 777, 'order': 10}]}
    steps = plugin.build_plan(group, {}, {}, {}, 'start')
    assert steps[0]['present'] is False
