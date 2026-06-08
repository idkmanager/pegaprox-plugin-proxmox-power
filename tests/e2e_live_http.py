# -*- coding: utf-8 -*-
"""Authenticated live E2E against a REAL PegaProx (CT119), v1.6.0 timing focus.

Read/dry-run only — no VM is mutated (execute runs in default dry-run). Creds and
base URL come from the environment so nothing is hard-coded:

  PP_BASE   e.g. https://190.160.10.212
  PP_USER   admin username
  PP_PASS   admin password

Run:  PP_BASE=... PP_USER=... PP_PASS=... python tests/e2e_live_http.py
"""
import os
import sys
import json
import urllib3
import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

urllib3.disable_warnings()

BASE = os.environ['PP_BASE'].rstrip('/')
USER = os.environ['PP_USER']
PASS = os.environ['PP_PASS']
PID = 'proxmox-power'
API = f'{BASE}/api/plugins/{PID}/api'

s = requests.Session()
s.verify = False
s.headers.update({'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json'})

PASSED, FAILED = [], []


def ok(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + extra) if extra else ''}")


def jget(path):
    r = s.get(f'{API}/{path}')
    return r.status_code, (r.json() if r.headers.get('content-type', '').startswith('application/json') else {})


def jpost(path, body):
    r = s.post(f'{API}/{path}', data=json.dumps(body))
    return r.status_code, (r.json() if r.headers.get('content-type', '').startswith('application/json') else {})


def main():
    print(f"E2E live HTTP — {BASE} (v1.6.0 timing)")

    # --- login ---
    r = s.post(f'{BASE}/api/auth/login', data=json.dumps({'username': USER, 'password': PASS}))
    ok('login', r.status_code == 200, f'HTTP {r.status_code}')
    if r.status_code != 200:
        return finish()

    # --- live reload so the new __init__.py is re-imported ---
    rr = s.post(f'{BASE}/api/plugins/{PID}/reload')
    ok('reload plugin', rr.status_code in (200, 204), f'HTTP {rr.status_code}')

    # --- version via update/check ---
    code, j = jget('update/check')
    ok('version 1.8.0', j.get('current') == '1.8.0', f"current={j.get('current')}")

    # --- clusters + inventory ---
    code, j = jget('clusters')
    clusters = j.get('clusters', [])
    ok('clusters listed', code == 200 and len(clusters) >= 1, f'{len(clusters)} clusters')
    cid = clusters[0]['id']
    code, j = jget(f'inventory?cluster_id={cid}')
    vms = j.get('vms', [])
    ok('inventory', code == 200 and len(vms) >= 1, f'{len(vms)} guests')
    running = [v for v in vms if v['status'] == 'running']
    stopped = [v for v in vms if v['status'] != 'running']
    pick = [v['vmid'] for v in (running[:1] + (stopped[:2] or running[1:3]))][:3]
    ok('picked members', len(pick) >= 2, str(pick))

    # --- save a phase group: member[0] phase 1, rest phase 2 (parallel) ---
    members = [{'vmid': pick[0], 'order': 1, 'health': {'mode': 'agent'}}]
    for v in pick[1:]:
        members.append({'vmid': v, 'order': 2, 'health': {'mode': 'status'},
                        'depends_on': [pick[0]]})
    grp = {'id': 'e2e-timing', 'name': 'E2E timing', 'cluster_id': cid,
           'settings': {'stop_mode': 'shutdown', 'step_timeout_sec': 300,
                        'storage_wait_sec': 120, 'storage_ready_sec': 45}, 'members': members}
    code, j = jpost('config/save', {'groups': [grp]})
    ok('config/save phase group', code == 200, json.dumps(j)[:120])
    # storage_ready_sec round-trips (spec 6/7 sequence-level gate)
    code, cfgj = jget('config')
    saved_g = next((g for g in cfgj.get('groups', []) if g['id'] == 'e2e-timing'), {})
    ok('storage_ready_sec persisted', saved_g.get('settings', {}).get('storage_ready_sec') == 45)

    # --- plan start: assert timing present + correct shape ---
    code, j = jpost('plan', {'cluster_id': cid, 'group': 'e2e-timing', 'action': 'start'})
    steps = j.get('steps', [])
    timing = j.get('timing', {})
    ok('plan start ok', code == 200 and len(steps) == len(members))
    ok('per-step timing', all('timing' in st and 'est_min' in st['timing'] for st in steps),
       'each step has timing.est_min')
    ok('plan timing summary', all(k in timing for k in ('est_sec', 'max_sec', 'est_min', 'max_min', 'phases')),
       f"≈ {timing.get('est_min')} min (máx {timing.get('max_min')} min)")
    phases = timing.get('phases', [])
    ok('phases aggregated', len(phases) == 2, f'{len(phases)} phases')
    # phase 2 must be flagged parallel when >1 member share it
    if len(members) > 2:
        ph2 = next((p for p in phases if p['phase'] == 2), {})
        ok('phase 2 parallel', ph2.get('parallel') is True and ph2.get('count') == len(members) - 1)
    # total est = sum of phase est (sequential phases)
    ok('total == sum of phases',
       timing.get('est_sec') == sum(p['est_sec'] for p in phases),
       f"{timing.get('est_sec')} == {sum(p['est_sec'] for p in phases)}")

    # --- plan stop: timing also present, reversed ---
    code, j = jpost('plan', {'cluster_id': cid, 'group': 'e2e-timing', 'action': 'stop'})
    ok('plan stop timing', code == 200 and 'est_min' in j.get('timing', {}),
       f"≈ {j.get('timing', {}).get('est_min')} min")

    # --- execute dry-run (default): job carries real elapsed in minutes ---
    code, j = jpost('execute', {'cluster_id': cid, 'group': 'e2e-timing', 'action': 'start'})
    ok('execute dry-run default', code == 200 and j.get('dry_run') is True, f"job={j.get('job_id')}")
    ok('execute returns timing', 'est_min' in j.get('timing', {}))
    job_id = j.get('job_id')
    # poll until done
    import time
    jb = {}
    for _ in range(40):
        code, jb = jget(f'job?id={job_id}')
        if jb.get('status') != 'running':
            break
        time.sleep(0.5)
    ok('job finished', jb.get('status') in ('done', 'failed'), f"status={jb.get('status')}")
    ok('job elapsed minutes', jb.get('elapsed_min') is not None and jb.get('elapsed_sec') is not None,
       f"⏱ {jb.get('elapsed_min')} min ({jb.get('elapsed_sec')} s)")
    jsteps = jb.get('steps', [])
    ok('per-step elapsed', all(st.get('elapsed_sec') is not None for st in jsteps),
       f'{len(jsteps)} steps timed')

    # --- jobs list exposes duration ---
    code, j = jget('jobs')
    jobs = j.get('jobs', [])
    mine = next((x for x in jobs if x['id'] == job_id), {})
    ok('jobs list duration', 'elapsed_min' in mine, f"elapsed_min={mine.get('elapsed_min')}")

    # --- autostart (unattended boot) — opt-in, never fires now ---
    code, j = jget('autostart/config')
    ok('autostart/config', code == 200 and 'settings' in j,
       f"enabled={j.get('settings', {}).get('enabled')}")
    # save: arm for NEXT boot only (saving does not power anything on now)
    code, j = jpost('autostart/save', {'autostart': {
        'enabled': True, 'cluster_id': cid, 'groups': ['e2e-timing'],
        'delay_sec': 0, 'wait_cluster_sec': 30, 'storage_ready_sec': 600}})
    ok('autostart/save persists', code == 200 and j.get('autostart', {}).get('groups') == ['e2e-timing'])
    ok('autostart storage_ready_sec', j.get('autostart', {}).get('storage_ready_sec') == 600)
    code, j = jpost('autostart/save', {'autostart': {'enabled': True, 'groups': ['does-not-exist']}})
    ok('autostart/save rejects unknown group', code == 400, f'HTTP {code}')
    # run now in DRY-RUN (default): previews the unattended boot, no VM mutated
    code, j = jpost('autostart/run', {})
    ok('autostart/run dry-run', code == 200 and j.get('dry_run') is True,
       f"{len(j.get('results', []))} group(s)")
    asjob = (j.get('results') or [{}])[0].get('job')
    if asjob:
        jb = {}
        for _ in range(40):
            code, jb = jget(f'job?id={asjob}')
            if jb.get('status') != 'running':
                break
            time.sleep(0.5)
        ok('autostart job done + dry-run', jb.get('status') in ('done', 'failed') and jb.get('dry_run') is True,
           f"status={jb.get('status')}")
    # disarm so we leave the box exactly as we found it
    code, j = jpost('autostart/save', {'autostart': {'enabled': False, 'groups': []}})
    ok('autostart disarmed', code == 200 and j.get('autostart', {}).get('enabled') is False)

    # --- cleanup: remove the e2e group ---
    code, j = jpost('config/save', {'groups': []})
    ok('cleanup config', code == 200)

    finish()


def finish():
    print(f"\n{'='*50}\n  PASSED {len(PASSED)} · FAILED {len(FAILED)}")
    if FAILED:
        print('  FAILS: ' + ', '.join(FAILED))
        sys.exit(1)
    print('  ALL GREEN ✅')


if __name__ == '__main__':
    main()
