# -*- coding: utf-8 -*-
"""Tests for the opt-in unattended boot (autostart on PegaProx ready)."""

import json


class _Mgr:
    """Tiny connected-manager stand-in (autostart only needs is_connected +
    the GET endpoints fetch_inventory/_collect_plan_inputs touch)."""
    host = '127.0.0.1'
    api_port = 8006
    is_connected = True

    class _R:
        status_code = 200
        text = ''

        def json(self):
            return {'data': []}

    def _api_get(self, url):
        return self._R()

    def _api_post(self, url, **kw):
        return self._R()


def _point_config(plugin, tmp_path, monkeypatch, cfg):
    p = tmp_path / 'config.json'
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(plugin, 'CONFIG_PATH', str(p))
    return str(p)


# --- settings ---------------------------------------------------------------

def test_autostart_disabled_by_default(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch, {'groups': []})
    s = plugin._autostart_settings()
    assert s['enabled'] is False and s['groups'] == [] and s['delay_sec'] == 30


# --- once-per-boot dedupe ---------------------------------------------------

def test_autostart_fires_once_per_boot_then_again_on_new_boot(plugin, tmp_path, monkeypatch):
    state_file = tmp_path / 'as_state.json'
    monkeypatch.setattr(plugin, '_autostart_state_path', lambda: str(state_file))
    monkeypatch.setattr(plugin, '_current_boot_id', lambda: 'boot-A')
    _point_config(plugin, tmp_path, monkeypatch,
                  {'groups': [], 'autostart': {'enabled': True, 'groups': ['g'], 'delay_sec': 0}})
    calls = []
    monkeypatch.setattr(plugin, '_run_autostart_groups',
                        lambda *a, **k: (calls.append(1) or [{'group': 'g', 'status': 'done'}]))

    plugin._autostart_runner()          # first boot -> runs
    plugin._autostart_runner()          # same boot -> skipped (reload safety)
    assert len(calls) == 1
    st = json.loads(state_file.read_text())
    assert st['completed'] is True and st['boot_id'] == 'boot-A'
    assert st['results'][0]['group'] == 'g'

    monkeypatch.setattr(plugin, '_current_boot_id', lambda: 'boot-B')  # rebooted
    plugin._autostart_runner()
    assert len(calls) == 2              # new boot -> fires again


def test_autostart_runner_noop_when_disabled(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, '_autostart_state_path', lambda: str(tmp_path / 's.json'))
    monkeypatch.setattr(plugin, '_current_boot_id', lambda: 'boot-X')
    _point_config(plugin, tmp_path, monkeypatch,
                  {'autostart': {'enabled': False, 'groups': ['g']}})
    calls = []
    monkeypatch.setattr(plugin, '_run_autostart_groups', lambda *a, **k: calls.append(1))
    plugin._autostart_runner()
    assert calls == []


# --- group orchestration ----------------------------------------------------

def test_run_autostart_groups_skips_unknown_group(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch,
                  {'groups': [], 'autostart': {'enabled': True, 'groups': ['ghost']}})
    res = plugin._run_autostart_groups(plugin._autostart_settings(), sync=True)
    assert res == [{'group': 'ghost', 'status': 'skipped', 'detail': 'group not found'}]


def test_run_autostart_groups_skips_when_no_cluster(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch, {
        'groups': [{'id': 'g', 'members': [{'vmid': 100, 'order': 1}]}],  # no cluster_id
        'autostart': {'enabled': True, 'groups': ['g'], 'cluster_id': ''}})
    res = plugin._run_autostart_groups(plugin._autostart_settings(), sync=True)
    assert res[0]['status'] == 'skipped' and 'no cluster_id' in res[0]['detail']


def test_run_autostart_groups_dry_run_creates_job(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch, {
        'groups': [{'id': 'g', 'cluster_id': 'c1', 'members': [{'vmid': 100, 'order': 1}]}],
        'autostart': {'enabled': True, 'groups': ['g'], 'cluster_id': 'c1', 'wait_cluster_sec': 1}})
    monkeypatch.setattr(plugin, 'get_connected_manager', lambda cid: (_Mgr(), None))
    res = plugin._run_autostart_groups(plugin._autostart_settings(), dry_run=True, sync=True)
    assert res[0]['group'] == 'g' and res[0]['status'] == 'done' and 'job' in res[0]


def test_run_autostart_injects_storage_ready_into_group(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch, {
        'groups': [{'id': 'g', 'cluster_id': 'c1',
                    'settings': {'storage_ready_sec': 10},
                    'members': [{'vmid': 100, 'order': 1}]}],
        'autostart': {'enabled': True, 'groups': ['g'], 'storage_ready_sec': 600,
                      'wait_cluster_sec': 1}})
    monkeypatch.setattr(plugin, 'get_connected_manager', lambda cid: (_Mgr(), None))
    captured = {}

    def fake_dispatch(manager, cluster_id, group, action, dry_run, username, sync=False):
        captured['settings'] = group.get('settings')
        return {'id': 'job-x', 'status': 'done'}, []

    monkeypatch.setattr(plugin, '_dispatch_group', fake_dispatch)
    plugin._run_autostart_groups(plugin._autostart_settings(), sync=True)
    # autostart-wide 600 wins over the group's own 10 (max)
    assert captured['settings']['storage_ready_sec'] == 600


def test_run_autostart_groups_fails_when_manager_never_connects(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch, {
        'groups': [{'id': 'g', 'cluster_id': 'c1', 'members': [{'vmid': 100, 'order': 1}]}],
        'autostart': {'enabled': True, 'groups': ['g'], 'wait_cluster_sec': 0}})
    monkeypatch.setattr(plugin, 'get_connected_manager', lambda cid: (None, 'not connected'))
    res = plugin._run_autostart_groups(plugin._autostart_settings(), sync=True)
    assert res[0]['status'] == 'failed' and 'not connected' in res[0]['detail']


# --- save handler validation ------------------------------------------------

def _set_body(plugin, monkeypatch, body):
    monkeypatch.setattr(plugin.request, 'get_json', lambda silent=False: body, raising=False)
    monkeypatch.setattr(plugin.request, 'session', {'user': 'tester'}, raising=False)


def test_autostart_save_rejects_unknown_group(plugin, tmp_path, monkeypatch):
    _point_config(plugin, tmp_path, monkeypatch, {'groups': []})
    _set_body(plugin, monkeypatch, {'autostart': {'enabled': True, 'groups': ['nope']}})
    r = plugin.autostart_save_handler()
    assert r[1] == 400 and 'unknown' in r[0][1]['error']


def test_autostart_save_persists_and_clamps(plugin, tmp_path, monkeypatch):
    path = _point_config(plugin, tmp_path, monkeypatch,
                         {'groups': [{'id': 'g', 'members': []}]})
    _set_body(plugin, monkeypatch, {'autostart': {
        'enabled': True, 'groups': ['g'], 'cluster_id': 'c1',
        'delay_sec': -5, 'wait_cluster_sec': 120, 'stop_on_error': True}})
    r = plugin.autostart_save_handler()
    assert r[0] == 'JSON'                       # success (no status tuple)
    saved = json.loads(open(path).read())['autostart']
    assert saved['enabled'] is True and saved['groups'] == ['g']
    assert saved['delay_sec'] == 0              # clamped to >= 0
    assert saved['stop_on_error'] is True


def test_autostart_save_can_disable_with_unknown_groups(plugin, tmp_path, monkeypatch):
    # When disabled, unknown groups don't block saving (operator turning it off).
    path = _point_config(plugin, tmp_path, monkeypatch, {'groups': []})
    _set_body(plugin, monkeypatch, {'autostart': {'enabled': False, 'groups': ['ghost']}})
    r = plugin.autostart_save_handler()
    assert r[0] == 'JSON'
    assert json.loads(open(path).read())['autostart']['enabled'] is False
