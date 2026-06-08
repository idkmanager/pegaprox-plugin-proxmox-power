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

    def __init__(self, storage=None, status_script=None, nodes=None):
        self.host = '127.0.0.1'
        self.api_port = 8006
        self.is_connected = True
        self.storage = storage or {}
        self.status_script = status_script or {}
        self.nodes = nodes or {'pve1': 'online'}
        self.ha = {}          # node -> 'online'|'maintenance'
        self.calls = []
        self._idx = {}        # per-instance status-script cursor (parallel-safe)

    def _api_get(self, url):
        self.calls.append(('GET', url))
        if url.endswith('/nodes'):
            return FakeResponse(200, [{'node': n, 'status': s} for n, s in self.nodes.items()])
        if url.endswith('/manager_status'):
            return FakeResponse(200, {'manager_status': {'node_status': dict(self.ha)}})
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
    # iscsi storage stays inactive -> _wait_storage times out -> step fails.
    # Guest reports stopped so the live re-check lets the start proceed to the gate.
    mgr = FakeManager(status_script={100: ['stopped']},
                      storage={'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}})
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


def test_live_recheck_skips_start_if_already_running(plugin):
    # Plan was built when the VM was stopped, but it came up since (e.g. a
    # dependency started it). Live re-check must skip the start, not error.
    mgr = FakeManager(status_script={100: ['running']})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    assert steps[0]['noop'] is False  # plan thought it was stopped
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'skipped'
    assert 'already running' in job['steps'][0]['detail']
    assert not any(u.endswith('/status/start') for _, u in mgr.calls)


def test_storage_policy_skip_skips_guest(plugin):
    mgr = FakeManager(status_script={100: ['stopped']},
                      storage={'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1},
             'members': [{'vmid': 100, 'order': 10, 'storage_policy': 'skip'}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    cfgs = {100: {'scsi0': 'lun0:vm-100-disk-0'}}
    storage = {'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}}
    steps = plugin.build_plan(group, inv, cfgs, storage, 'start')
    assert steps[0]['storage_policy'] == 'skip'
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'skipped'
    assert job['status'] == 'done'  # skip is not a failure
    assert not any(u.endswith('/status/start') for _, u in mgr.calls)


def test_storage_policy_fail_does_not_wait(plugin):
    mgr = FakeManager(status_script={100: ['stopped']},
                      storage={'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1,
                                     'storage_wait_sec': 999},
             'members': [{'vmid': 100, 'order': 10, 'storage_policy': 'fail'}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    cfgs = {100: {'scsi0': 'lun0:vm-100-disk-0'}}
    storage = {'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 0}}}
    steps = plugin.build_plan(group, inv, cfgs, storage, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)  # must NOT hang on storage_wait_sec
    assert job['steps'][0]['state'] == 'failed'
    assert 'storage not active' in job['steps'][0]['detail']


def test_node_in_maintenance_blocks_start(plugin):
    mgr = FakeManager(status_script={100: ['stopped']})
    mgr.ha = {'pve1': 'maintenance'}
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'failed'
    assert 'maintenance' in job['steps'][0]['detail']
    assert not any(u.endswith('/status/start') for _, u in mgr.calls)


def test_ignore_maintenance_allows_start(plugin):
    mgr = FakeManager(status_script={100: ['stopped', 'running']})
    mgr.ha = {'pve1': 'maintenance'}
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1,
                                     'step_timeout_sec': 3, 'ignore_maintenance': True},
             'members': [{'vmid': 100, 'order': 10, 'health': {'mode': 'status'}}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'done'
    assert any(u.endswith('/status/start') for _, u in mgr.calls)


def test_node_offline_blocks_start(plugin):
    mgr = FakeManager(status_script={100: ['stopped']}, nodes={'pve1': 'offline'})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 0},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'failed'
    assert 'not online' in job['steps'][0]['detail']


def test_start_guards_missing_node(plugin):
    mgr = FakeManager(status_script={100: ['stopped']})
    # inventory entry without a node (Proxmox omitted the field)
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': None, 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'failed'
    assert 'no node' in job['steps'][0]['detail']
    assert not any(u.endswith('/status/start') for _, u in mgr.calls)


def test_stop_fails_when_node_offline(plugin):
    mgr = FakeManager(status_script={100: ['running']}, nodes={'pve1': 'offline'})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 0},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'running'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'stop')
    job = _mk_job(plugin, 'stop', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'failed'
    assert 'not online' in job['steps'][0]['detail']
    assert not any(u.endswith('/status/shutdown') for _, u in mgr.calls)


def test_same_order_members_start_in_parallel(plugin):
    # Two members in the same phase (order) -> both started in one wave.
    mgr = FakeManager(status_script={534: ['stopped', 'running'], 535: ['stopped', 'running']})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1,
                                     'step_timeout_sec': 3},
             'members': [{'vmid': 534, 'order': 2, 'health': {'mode': 'status'}},
                         {'vmid': 535, 'order': 2, 'health': {'mode': 'status'}}]}
    inv = {534: {'node': 'pve1', 'name': 'a', 'type': 'qemu', 'status': 'stopped'},
           535: {'node': 'pve1', 'name': 'b', 'type': 'qemu', 'status': 'stopped'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    assert all(s['wave'] == 1 and s['parallel'] for s in steps)
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['status'] == 'done'
    assert any(u.endswith('/nodes/pve1/qemu/534/status/start') for _, u in mgr.calls)
    assert any(u.endswith('/nodes/pve1/qemu/535/status/start') for _, u in mgr.calls)


def test_required_storages_filters_noop_skip_absent(plugin):
    steps = [
        {'action': 'start', 'present': True, 'noop': False, 'storage_policy': 'wait',
         'node': 'pve1', 'storage': [{'storage': 'a'}]},
        {'action': 'start', 'present': True, 'noop': True, 'storage_policy': 'wait',
         'node': 'pve1', 'storage': [{'storage': 'b'}]},   # already running -> excluded
        {'action': 'start', 'present': True, 'noop': False, 'storage_policy': 'skip',
         'node': 'pve1', 'storage': [{'storage': 'c'}]},    # skip -> excluded
        {'action': 'start', 'present': False, 'noop': False, 'storage_policy': 'wait',
         'node': 'pve2', 'storage': [{'storage': 'd'}]},    # absent -> excluded
    ]
    assert plugin._required_storages(steps) == {'pve1': {'a'}}


def test_wait_storage_ready_ok_when_active(plugin):
    mgr = FakeManager(storage={'pve1': {'a': {'type': 'nfs', 'enabled': 1, 'active': 1, 'shared': 1}}})
    steps = [{'action': 'start', 'present': True, 'noop': False, 'storage_policy': 'wait',
              'node': 'pve1', 'storage': [{'storage': 'a'}]}]
    ok, err = plugin._wait_storage_ready(mgr, steps, 1, 0)
    assert ok is True and err is None


def test_wait_storage_ready_times_out_when_inactive(plugin):
    mgr = FakeManager(storage={'pve1': {'a': {'type': 'nfs', 'enabled': 1, 'active': 0}}})
    steps = [{'action': 'start', 'present': True, 'noop': False, 'storage_policy': 'wait',
              'node': 'pve1', 'storage': [{'storage': 'a'}]}]
    ok, err = plugin._wait_storage_ready(mgr, steps, 0, 0)
    assert ok is False and 'pve1:a' in err


def test_execute_job_waits_storage_live_before_sequence(plugin):
    # storage_ready_sec>0: the sequence holds for storage, then starts the VM.
    mgr = FakeManager(status_script={100: ['stopped', 'running']},
                      storage={'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 1}}})
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0, 'host_wait_sec': 1,
                                     'step_timeout_sec': 3, 'storage_ready_sec': 5},
             'members': [{'vmid': 100, 'order': 1, 'health': {'mode': 'status'}}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'stopped'}}
    cfgs = {100: {'scsi0': 'lun0:vm-100-disk-0'}}
    storage = {'pve1': {'lun0': {'type': 'iscsi', 'enabled': 1, 'active': 1}}}
    steps = plugin.build_plan(group, inv, cfgs, storage, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['status'] == 'done'
    assert any('storage live' in l['msg'] for l in job['log'])
    assert any(u.endswith('/status/start') for _, u in mgr.calls)


def test_noop_running_vm_is_skipped(plugin):
    mgr = FakeManager()
    group = {'id': 'g', 'settings': {'poll_interval_sec': 0},
             'members': [{'vmid': 100, 'order': 10}]}
    inv = {100: {'node': 'pve1', 'name': 'db', 'type': 'qemu', 'status': 'running'}}
    steps = plugin.build_plan(group, inv, {}, {}, 'start')
    job = _mk_job(plugin, 'start', dry_run=False)
    plugin._execute_job(job, mgr, group, inv, steps)
    assert job['steps'][0]['state'] == 'skipped'
