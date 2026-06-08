# -*- coding: utf-8 -*-
"""Stub the PegaProx host modules so the plugin imports standalone in CI.

The plugin only needs a handful of names from ``pegaprox.*`` at import time
(register_plugin_route, helpers, auth, rbac, audit) plus ``flask``. We register
lightweight fakes in ``sys.modules`` *before* the plugin is imported, then load
``__init__.py`` as the module ``proxmox_power`` for the tests to use.
"""

import os
import sys
import types
import importlib.util

import pytest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def _install_fakes():
    # flask: only request/jsonify/send_file are touched at import + handler time.
    if 'flask' not in sys.modules:
        flask = _mod('flask')

        def jsonify(obj=None, **kw):
            return ('JSON', obj if obj is not None else kw)

        def send_file(path, mimetype=None):
            return ('FILE', path, mimetype)

        flask.jsonify = jsonify
        flask.send_file = send_file
        flask.request = types.SimpleNamespace(
            args={}, method='GET', session={'user': 'tester'},
            get_json=lambda silent=False: {},
        )

    # pegaprox package tree
    _mod('pegaprox')
    api = _mod('pegaprox.api')
    sys.modules['pegaprox'].api = api
    plugins = _mod('pegaprox.api.plugins')
    plugins.register_plugin_route = lambda *a, **k: None
    helpers = _mod('pegaprox.api.helpers')
    helpers.get_connected_manager = lambda cid: (None, None)
    helpers.check_cluster_access = lambda cid: (True, None)
    helpers.safe_error = lambda e, default='err': str(e) or default
    utils = _mod('pegaprox.utils')
    sys.modules['pegaprox'].utils = utils
    auth = _mod('pegaprox.utils.auth')
    auth.load_users = lambda: {'tester': {'role': 'admin'}}
    rbac = _mod('pegaprox.utils.rbac')
    rbac.has_permission = lambda user, perm, tenant_id=None: True
    audit = _mod('pegaprox.utils.audit')
    audit.log_audit = lambda **k: None
    globals_mod = _mod('pegaprox.globals')
    globals_mod.cluster_managers = {}


# Install stubs at import time so any collection-time import of the plugin's
# __init__.py (flask / pegaprox.*) resolves against the fakes, not the host env.
_install_fakes()


@pytest.fixture(scope='session')
def plugin():
    _install_fakes()
    spec = importlib.util.spec_from_file_location(
        'proxmox_power', os.path.join(PLUGIN_ROOT, '__init__.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules['proxmox_power'] = mod
    spec.loader.exec_module(mod)
    return mod
