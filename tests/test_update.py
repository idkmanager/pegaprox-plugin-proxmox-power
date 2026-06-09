# -*- coding: utf-8 -*-
"""Tests for the auto-update logic (version compare + check/apply with a
monkeypatched fetch — no network)."""

import json
import os


def test_version_tuple(plugin):
    assert plugin.version_tuple('1.2.3') == (1, 2, 3)
    assert plugin.version_tuple('1.0') == (1, 0)
    assert plugin.version_tuple('v2.0.1') == (2, 0, 1)
    assert plugin.version_tuple(None) == (0,)


def test_version_gt(plugin):
    assert plugin.version_gt('1.1.0', '1.0.3') is True
    assert plugin.version_gt('1.0.10', '1.0.9') is True
    assert plugin.version_gt('2.0', '1.9.9') is True
    assert plugin.version_gt('1.0.0', '1.0.0') is False
    assert plugin.version_gt('1.0.0', '1.1.0') is False


def test_check_update_available(plugin, monkeypatch):
    monkeypatch.setattr(plugin, '_local_version', lambda: '1.0.0')
    monkeypatch.setattr(plugin, '_fetch_remote_text',
                        lambda src, name, timeout=10: json.dumps({'version': '1.2.0'}))
    r = plugin.check_update('http://example/repo')
    assert r['update_available'] is True
    assert r['latest'] == '1.2.0' and r['current'] == '1.0.0'


def test_check_update_none_when_same(plugin, monkeypatch):
    monkeypatch.setattr(plugin, '_local_version', lambda: '1.2.0')
    monkeypatch.setattr(plugin, '_fetch_remote_text',
                        lambda src, name, timeout=10: json.dumps({'version': '1.2.0'}))
    assert plugin.check_update('http://x')['update_available'] is False


def test_check_update_network_error_is_soft(plugin, monkeypatch):
    monkeypatch.setattr(plugin, '_local_version', lambda: '1.0.0')

    def boom(src, name, timeout=10):
        raise RuntimeError('dns fail')
    monkeypatch.setattr(plugin, '_fetch_remote_text', boom)
    r = plugin.check_update('http://x')
    assert r['update_available'] is False and 'error' in r


def test_apply_update_validates_and_writes(plugin, monkeypatch, tmp_path):
    # Point PLUGIN_DIR at a temp dir with a current manifest.
    monkeypatch.setattr(plugin, 'PLUGIN_DIR', str(tmp_path))
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': '1.0.0'}))
    (tmp_path / '__init__.py').write_text('# old\n')
    (tmp_path / 'power.html').write_text('<html>old</html>')

    files = {
        'manifest.json': json.dumps({'name': 'x', 'version': '1.5.0'}),
        '__init__.py': '# new valid python\nX = 1\n',
        'power.html': '<html>new</html>',
    }
    monkeypatch.setattr(plugin, '_fetch_remote_text',
                        lambda src, name, timeout=10: files[name])
    res = plugin.apply_update('http://x')
    assert res == {'applied': True, 'from': '1.0.0', 'to': '1.5.0', 'source': 'http://x'}
    assert json.loads((tmp_path / 'manifest.json').read_text())['version'] == '1.5.0'
    assert 'new valid python' in (tmp_path / '__init__.py').read_text()
    assert (tmp_path / 'manifest.json.bak').exists()  # backup kept


def test_apply_update_rejects_broken_python(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, 'PLUGIN_DIR', str(tmp_path))
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': '1.0.0'}))
    (tmp_path / '__init__.py').write_text('# old\n')
    (tmp_path / 'power.html').write_text('<html>old</html>')

    files = {
        'manifest.json': json.dumps({'version': '2.0.0'}),
        '__init__.py': 'def broken(:\n  syntax error',   # invalid
        'power.html': '<html>new</html>',
    }
    monkeypatch.setattr(plugin, '_fetch_remote_text',
                        lambda src, name, timeout=10: files[name])
    import pytest
    with pytest.raises(Exception):
        plugin.apply_update('http://x')
    # nothing was overwritten
    assert '# old' in (tmp_path / '__init__.py').read_text()
    assert json.loads((tmp_path / 'manifest.json').read_text())['version'] == '1.0.0'


def test_apply_update_refuses_downgrade(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, 'PLUGIN_DIR', str(tmp_path))
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': '1.5.0'}))
    (tmp_path / '__init__.py').write_text('# old\n')
    (tmp_path / 'power.html').write_text('<html>old</html>')
    files = {'manifest.json': json.dumps({'version': '1.0.0'}),
             '__init__.py': 'X=1\n', 'power.html': '<html>new</html>'}
    monkeypatch.setattr(plugin, '_fetch_remote_text',
                        lambda src, name, timeout=10: files[name])
    import pytest
    with pytest.raises(Exception, match='downgrade'):
        plugin.apply_update('http://x')
    # nothing overwritten
    assert json.loads((tmp_path / 'manifest.json').read_text())['version'] == '1.5.0'
    # ...but forced downgrade is allowed
    res = plugin.apply_update('http://x', allow_downgrade=True)
    assert res['to'] == '1.0.0'


def test_check_update_falls_back_to_mirror(plugin, monkeypatch):
    # Primary (raw.githubusercontent) unreachable -> jsDelivr mirror answers.
    monkeypatch.setattr(plugin, '_local_version', lambda: '1.0.0')
    monkeypatch.setattr(plugin, '_load_config', lambda: {'groups': []})  # pure defaults

    def fetch(src, name, timeout=10):
        if 'raw.githubusercontent.com' in src:
            raise RuntimeError('NameResolutionError')
        return json.dumps({'version': '1.2.0'})
    monkeypatch.setattr(plugin, '_fetch_remote_text', fetch)
    r = plugin.check_update()
    assert r['update_available'] is True and r['latest'] == '1.2.0'
    assert 'jsdelivr' in (r['source'] or '')


def test_apply_update_falls_back_to_mirror(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, 'PLUGIN_DIR', str(tmp_path))
    monkeypatch.setattr(plugin, '_load_config', lambda: {'groups': []})
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': '1.0.0'}))
    (tmp_path / '__init__.py').write_text('# old\n')
    (tmp_path / 'power.html').write_text('<html>old</html>')
    files = {'manifest.json': json.dumps({'version': '1.5.0'}),
             '__init__.py': 'X = 1\n', 'power.html': '<html>new</html>'}

    def fetch(src, name, timeout=10):
        if 'raw.githubusercontent.com' in src:
            raise RuntimeError('dns fail')
        return files[name]
    monkeypatch.setattr(plugin, '_fetch_remote_text', fetch)
    res = plugin.apply_update()
    assert res['applied'] and res['to'] == '1.5.0' and 'jsdelivr' in res['source']


def test_apply_update_rejects_empty_html(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, 'PLUGIN_DIR', str(tmp_path))
    (tmp_path / 'manifest.json').write_text(json.dumps({'version': '1.0.0'}))
    (tmp_path / '__init__.py').write_text('# old\n')
    (tmp_path / 'power.html').write_text('<html>old</html>')
    files = {'manifest.json': json.dumps({'version': '2.0.0'}),
             '__init__.py': 'X=1\n', 'power.html': '   '}
    monkeypatch.setattr(plugin, '_fetch_remote_text',
                        lambda src, name, timeout=10: files[name])
    import pytest
    with pytest.raises(Exception):
        plugin.apply_update('http://x')
    assert '<html>old</html>' in (tmp_path / 'power.html').read_text()
