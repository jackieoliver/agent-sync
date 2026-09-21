#!/usr/bin/env python3
"""Regroup Codex desktop threads into projects by their working folder.

Codex groups sidebar threads only through explicit per-thread assignments in
~/.codex/.codex-global-state.json ("thread-project-assignments"), written when a
thread is created or imported in a folder that exactly equals a project root.
Threads without an assignment show as ungrouped, and threads synced from the
other machine carry a foreign folder, so they can never be assigned by the app.

This script (run with Codex QUIT; the app rewrites the whole state file on any
change) assigns every unassigned thread whose folder, after the Mac<->Linux
prefix swap, exactly matches a project root. Folders that exist on this machine
but have no project get one created. With --placeholders, foreign folders that
do not exist here get a project too, named after the folder, so those threads
still group by repo. Never deletes or reassigns an existing assignment.
Python 3.9 compatible. Usage: codex_regroup.py [--apply] [--placeholders]
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid

HOME = os.path.expanduser('~')
STATE = os.path.join(HOME, '.codex', '.codex-global-state.json')
SIDES = {
    'linux': {'home': '/home/haptica', 'github': '/home/haptica/GitHub'},
    'mac': {'home': '/Users/jacquelineoliver', 'github': '/Users/jacquelineoliver/Documents/GitHub'},
}
ME = 'mac' if sys.platform == 'darwin' else 'linux'
OTHER = 'mac' if ME == 'linux' else 'linux'


def norm(s):
    return ''.join(ch if ch.isalnum() else '-' for ch in s.lower()).strip('-')


def map_cwd(cwd):
    """Translate a foreign folder to this machine, or return cwd unchanged if native."""
    s, d = SIDES[OTHER], SIDES[ME]
    if cwd.startswith(d['home'] + '/') or cwd == d['home']:
        return cwd
    if cwd == s['github']:
        return d['github']
    if cwd.startswith(s['github'] + '/'):
        parts = cwd[len(s['github']) + 1:].split('/')
        if os.path.isdir(d['github']):
            for name in os.listdir(d['github']):
                if norm(name) == norm(parts[0]):
                    return '/'.join([d['github'], name] + parts[1:])
        return '/'.join([d['github']] + parts)  # not present here; may become a placeholder
    if cwd == s['home']:
        return d['home']
    if cwd.startswith(s['home'] + '/'):
        return d['home'] + cwd[len(s['home']):]
    return cwd


def eligible(local):
    """Only real project folders get projects: repos (and their subfolders) under the GitHub root,
    the home and GitHub roots themselves, and named folders directly under home or Documents.
    Never Codex's own per-thread folders (~/Documents/Codex/<date>/<slug>), scratch/tmp/worktree dirs,
    or UUID-named folders."""
    d = SIDES[ME]
    base = os.path.basename(local.rstrip('/'))
    if len(base) == 36 and base.count('-') == 4:
        return False
    for bad in ('/Documents/Codex/', '/scratch-workspaces/', '/.codex/worktrees/', '/.claude/worktrees/',
                '/tmp/', '/scratchpad', '/codex-logs/', '/.claude-mem/'):
        if bad in local + '/':
            return False
    if local in (d['home'], d['github']):
        return True
    if local.startswith(d['github'] + '/'):
        return True
    parent = os.path.dirname(local)
    return parent in (d['home'], os.path.join(d['home'], 'Documents'))


def uuid7():
    """Time-ordered UUID like the ids Codex's app server generates."""
    ms = int(time.time() * 1000)
    rnd = uuid.uuid4().bytes
    b = ms.to_bytes(6, 'big') + bytes([0x70 | (rnd[6] & 0x0F), rnd[7], 0x80 | (rnd[8] & 0x3F)]) + rnd[9:16]
    return str(uuid.UUID(bytes=b))


def mirror_to_sqlite(db, state, host_key, new_projects, assignments):
    """Write project rows and thread.project_id into Codex's new store, which the sidebar reads.
    Legacy ids map to app-server ids through the state's id map; new projects get both."""
    idmap = state.setdefault('app-server-project-id-by-legacy-project-id-by-host', {}).setdefault(host_key, {})
    con = sqlite3.connect(db)
    try:
        now = int(time.time() * 1000)
        pos = con.execute('select coalesce(max(position), -1) from projects').fetchone()[0]
        for legacy_id, proj in new_projects.items():
            app_id = uuid7(); pos += 1
            con.execute('insert into projects (id, name, metadata, position, created_at_ms, updated_at_ms) values (?,?,?,?,?,?)',
                        (app_id, proj['name'], '{}', pos, now, now))
            for i, root in enumerate(proj['rootPaths']):
                con.execute('insert into project_roots (project_id, position, path) values (?,?,?)', (app_id, i, root))
            idmap[legacy_id] = app_id
        known = {r[0] for r in con.execute('select id from projects')}
        written = missing = 0
        for tid, a in assignments.items():
            app_id = idmap.get(a.get('projectId'))
            if app_id and app_id in known:
                cur = con.execute('update threads set project_id = ? where id = ? and (project_id is null or project_id != ?)', (app_id, tid, app_id))
                written += cur.rowcount
            else:
                missing += 1
        con.commit()
    finally:
        con.close()
    return {'thread_links_written': written, 'assignments_without_app_project': missing}


def codex_running():
    names = ('ChatGPT', 'Codex') if ME == 'mac' else ('chatgpt', 'ChatGPT')
    return any(os.system('pgrep -x %s >/dev/null 2>&1' % n) == 0 for n in names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--placeholders', action='store_true', help='also create projects for foreign folders missing here')
    ap.add_argument('--names-from', help="JSON file with the other machine's local-projects; reuse its project names")
    a = ap.parse_args()
    other_names = {}
    if a.names_from:
        for proj in json.load(open(a.names_from)).values():
            for r in proj.get('rootPaths', []):
                other_names[os.path.normpath(map_cwd(r))] = proj['name']
    if a.apply and codex_running():
        print(json.dumps({'error': 'Codex is running; quit it first'}))
        return 2
    state = json.load(open(STATE))
    projects = state.setdefault('local-projects', {})
    assignments = state.setdefault('thread-project-assignments', {})
    order = state.setdefault('project-order', [])
    projectless = set(state.get('projectless-thread-ids', []))
    root_to_pid = {}
    for pid, p in projects.items():
        for r in p.get('rootPaths', []):
            root_to_pid.setdefault(os.path.normpath(r), pid)

    db = sorted(glob.glob(os.path.join(HOME, '.codex', 'state_*.sqlite')))[-1]
    con = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
    threads = con.execute('select id, cwd from threads where archived = 0').fetchall()

    plan = {'assign': {}, 'new_projects': {}, 'skipped_missing_folder': {}, 'already_assigned': 0}
    for tid, cwd in threads:
        if tid in assignments:
            plan['already_assigned'] += 1
            continue
        if not cwd:
            continue
        local = os.path.normpath(map_cwd(cwd))
        pid = root_to_pid.get(local)
        if not pid:
            if not eligible(local):
                plan['skipped_missing_folder'][local] = plan['skipped_missing_folder'].get(local, 0) + 1
                continue
            exists = os.path.isdir(local)
            if not exists and not a.placeholders:
                plan['skipped_missing_folder'][local] = plan['skipped_missing_folder'].get(local, 0) + 1
                continue
            pid = str(uuid.uuid4())
            name = other_names.get(local) or os.path.basename(local.rstrip('/')) or local
            if not exists:
                name = name + ' (' + OTHER + ')'
            plan['new_projects'][pid] = {'id': pid, 'name': name, 'rootPaths': [local], 'createdAt': 0, 'updatedAt': 0}
            root_to_pid[local] = pid
        plan['assign'][tid] = pid

    summary = {'threads': len(threads), 'already_assigned': plan['already_assigned'],
               'to_assign': len(plan['assign']),
               'new_projects': sorted(p['name'] for p in plan['new_projects'].values()),
               'skipped_missing_folder': dict(sorted(plan['skipped_missing_folder'].items(), key=lambda x: -x[1])[:12])}
    if not a.apply:
        idmap = state.get('app-server-project-id-by-legacy-project-id-by-host', {}).get('local:' + os.path.join(HOME, '.codex'), {})
        total = {**assignments, **{t: {'projectId': pid} for t, pid in plan['assign'].items()}}
        summary['links_to_write'] = sum(1 for a_ in total.values() if a_.get('projectId') in idmap or a_.get('projectId') in plan['new_projects'])
        summary['mode'] = 'dry-run'
        print(json.dumps(summary, indent=1))
        return 0
    stamp = time.strftime('%Y%m%dT%H%M%S')
    for f in (STATE, STATE + '.bak'):
        if os.path.exists(f):
            shutil.copy2(f, f + '.regroup-backup-' + stamp)
    projects.update(plan['new_projects'])
    for pid in plan['new_projects']:
        if pid not in order:
            order.append(pid)
    for tid, pid in plan['assign'].items():
        assignments[tid] = {'projectKind': 'local', 'projectId': pid}
        projectless.discard(tid)
    state['projectless-thread-ids'] = sorted(projectless)
    for ext in ('', '-wal', '-shm'):
        if os.path.exists(db + ext):
            shutil.copy2(db + ext, db + ext + '.regroup-backup-' + stamp)
    host_key = 'local:' + os.path.join(HOME, '.codex')
    summary['sqlite'] = mirror_to_sqlite(db, state, host_key, plan['new_projects'], assignments)
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)
    shutil.copy2(STATE, STATE + '.bak')
    summary['mode'] = 'applied'
    summary['backup'] = STATE + '.regroup-backup-' + stamp
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
