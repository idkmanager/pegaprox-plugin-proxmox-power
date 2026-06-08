# -*- coding: utf-8 -*-
"""Engine tests with a fake manager — exercise dry-run + real wait/power loops
without a live Proxmox."""

import pytest


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = ''

    def json(self):
        return {'data': self._data}


class FakeManager:
    """Minimal stand-in for a PegaProx cluster manager.

    ``status_script`` maps vmid -> list of statuses returned on successive
    status polls (last value repeats), so we can simulate a guest taking a few
    polls to come up.
    """

    def __init__(self, storage=None, status_script=None):
        self.host = '127.0.0.1'
        self.api_port = 8006
        self.is_connected = True
        self.storage = storage or {}
        self.status_script = status_script or {}
        self.calls = []

    def _get_idx(self, vmid):
        return self._idx.setdefault(vmid, 0)

    _idx = {}

    def _api_get(self, url):
        self.calls.append(('GET', url))
        if '/storage' in url:
            node = url.split('/nodes/')[1].split('/')[0]
            data = [dict(storage=s, **v) for s, v in self.storage.get(node, {}).items()]
            return FakeResponse(200, data)
        if '/status/current' in url:
            vmid = int(url.split('/')[-3])
            seq = self.status_script.get(vmid, ['running'])
            i = min(self._idx.get(vmid, 0), len(seq) - 1)
            self._idx[vmid] = self._idx.get(vmid, 0) + 1
            return FakeResponse(200, {'status': seq[i]})
        return FakeResponse(200, [])

    def _api_post(self, url, **kw):
        self.calls.append(('POST', url))
        return FakeResponse(200, {})


def _mk_job(plugin, action='start', dry_run=True):
    return plugin._new_job('c1', 'g', action, dry_run, 'tester')


def test_dry_run_simulates_without_power_calls(plugin):
    FakeManager._idx = {}
    mgr = FakeManager()
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=True)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['status'] == 'done'
    assert job['steps'][0]['state'] == 'simulated'
    # No power verbs were issued.
    assert not any(m == 'POST' and 'status/start' in u for m, u in mgr.calls)


def test_real_start_issues_power_and_waits_healthy(plugin):
    FakeManager._idx = {}
    # VM reports 'stopped' once, then 'running'.
    mgr = FakeManager(status_script={100: ['stopped', 'running']})
    group = {'id': 'g',
             'settings': {'poll_interval_sec': 0, 'step_timeout_sec': 5,
                          'storage_wait_sec': 5},
             'members': [{'vmid': 100, 'order': 10, 'health': {'mode': 'status'}}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['status'] == 'done'
    assert job['steps'][0]['state'] == 'done'
    assert any(m == 'POST' and u.endswith('/status/start') for m, u in mgr.calls)


def test_real_stop_uses_shutdown_and_waits_stopped(plugin):
    FakeManager._idx = {}
    mgr = FakeManager(status_script={100: ['running', 'stopped']})
    group = {'id': 'g',
             'settings': {'poll_interval_sec': 0, 'step_timeout_sec': 5,
                          'stop_mode': 'shutdown'},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'running'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'stop')
    job = _mk_job(plugin, 'stop', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['status'] == 'done'
    assert any(m == 'POST' and u.endswith('/status/shutdown') for m, u in mgr.calls)


def test_start_aborts_when_storage_never_activates(plugin):
    FakeManager._idx = {}
    # iscsi storage stays inactive -> _wait_storage times out -> step fails.
    mgr = FakeManager(storage={'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}})
    group = {'id': 'g',
             'settings': {'poll_interval_sec': 0, 'storage_wait_sec': 0,
                          'step_timeout_sec': 1, 'continue_on_error': False},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    cfgs = {100: {'scsi0': 'lun0:vm-100-disk-0'}}
    storage = {'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}}
    steps = plugin.build_plan(group, inv, cfgs, storage, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['status'] == 'failed'
    assert job['steps'][0]['state'] == 'failed'
    # Power start must NOT have been attempted once storage failed the gate.
    assert not any(u.endswith('/status/start') for _, u in mgr.calls)


def test_noop_running_vm_is_skipped(plugin):
    FakeManager._idx = {}
    mgr = FakeManager()
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'running'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'skipped'
