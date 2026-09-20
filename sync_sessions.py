#!/usr/bin/env python3
"""Mac history -> Linux; post-baseline conversations -> both devices."""
import argparse
from collections import Counter
import datetime
import fcntl
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

BASE = Path(__file__).resolve().parent
import mac_host
MAC = mac_host.target()  # Tailscale, else Ethernet; identity pinned in mac-known-hosts
from syncconf import CONF
REMOTE = CONF['mac_remote_dir']
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
       '-o', 'UserKnownHostsFile=' + str(BASE / 'mac-known-hosts')]
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def call(side, request):
    if side == 'linux':
        cmd = [sys.executable, str(BASE / 'session_endpoint.py'), '--state', str(BASE / 'state')]
    else:
        cmd = SSH + [MAC, shlex.join(['/usr/bin/python3', REMOTE + '/session_endpoint.py', '--state', REMOTE + '/state'])]
    p = subprocess.run(cmd, input=json.dumps(request), capture_output=True, text=True, timeout=900)
    if p.returncode:
        # Endpoint errors contain file paths but no conversation content.
        raise RuntimeError(side + ' endpoint failed: ' + p.stderr[-1500:])
    return json.loads(p.stdout)


def historical_ids():
    # Only conversations that existed on Linux before setup are barred outbound.
    # Imported Mac histories already exist on Mac: after the initial import their
    # future extensions can converge in either direction without leaking Linux's
    # historical conversations. The Mac baseline remains as an audit record.
    result = {'codex': set(), 'claude': set()}
    for side in ('linux',):
        data = json.loads((BASE / (side + '-baseline.json')).read_text())
        for agent in result:
            for relative in data[agent]:
                name = Path(relative).name
                matches = UUID.findall(name)
                if matches:
                    result[agent].add(matches[-1])
    return result


def logical_key(agent, relative, data):
    if agent == 'codex':
        return data['id'], ''
    parts = Path(relative).parts
    return data['id'], '/'.join(parts[3:]) if len(parts) > 3 else ''


def plan(source, destination, direction, historical):
    files, excluded, ambiguous = [], Counter(), Counter()
    for agent in ('codex', 'claude'):
        main_ids = {data['id'] for rel, data in source[agent].items()
                    if agent == 'codex' or len(Path(rel).parts) == 3}
        lookup = {}
        for rel, data in destination[agent].items():
            key = logical_key(agent, rel, data)
            lookup.setdefault(key, []).append((rel, data))
        for rel, data in source[agent].items():
            if direction == 'linux_to_mac' and data['id'] in historical[agent]:
                excluded[agent] += 1
                continue
            if direction == 'linux_to_mac' and data['id'] not in main_ids:
                # Old orphan attachments do not count as a new conversation.
                excluded[agent] += 1
                continue
            matches = lookup.get(logical_key(agent, rel, data), [])
            if len(matches) > 1:
                ambiguous[agent] += 1
                continue
            destrel, target = matches[0] if matches else (rel, None)
            if target and target['sha256'] == data['sha256']:
                continue
            if target and target['size'] > data['size']:
                continue
            item = dict(data, agent=agent, path=rel, destination=destrel,
                        expected=target['sha256'] if target else None)
            files.append(item)
    return files, dict(excluded), dict(ambiguous)


def transfer(source, destination, run, files):
    if not files:
        return {'applied': [], 'skipped': []}
    ready = call(source, {'op': 'prepare', 'run': run, 'files': files})
    selected = ready['files']
    if not selected:
        return {'applied': [], 'skipped': ready['skipped']}
    filelist = BASE / 'state' / ('files-' + run + '.txt')
    filelist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    filelist.write_bytes(b''.join((i['agent'] + '/' + i['path']).encode() + b'\0' for i in selected))
    filelist.chmod(0o600)
    local_in = BASE / 'state/incoming' / run
    local_in.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination == 'mac':
        subprocess.run(SSH + [MAC, shlex.join(['mkdir', '-p', REMOTE + '/state/incoming/' + run])], check=True, timeout=20)
        src = str(BASE / 'state/outgoing' / run) + '/'
        dst = MAC + ':' + REMOTE + '/state/incoming/' + run + '/'
    else:
        src = MAC + ':' + REMOTE + '/state/outgoing/' + run + '/'
        dst = str(local_in) + '/'
    subprocess.run(['rsync', '-rltz', '--from0', '--files-from=' + str(filelist),
                    '-e', shlex.join(SSH), src, dst], check=True, timeout=900,
                   stdout=subprocess.DEVNULL)
    result = call(destination, {'op': 'apply', 'run': run, 'files': selected})
    result['skipped'] += ready['skipped']
    return result


def count(items):
    return dict(Counter(item['agent'] for item in items))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    state = BASE / 'state'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / 'controller.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another synchronization is already running.')
            return
        historical = historical_ids()
        linux, mac = call('linux', {'op': 'inventory'}), call('mac', {'op': 'inventory'})
        run = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        incoming, _, ambiguous_in = plan(mac, linux, 'mac_to_linux', historical)
        outgoing, excluded, ambiguous_out = plan(linux, mac, 'linux_to_mac', historical)
        report = {'run': run, 'mode': 'apply' if args.apply else 'preview',
                  'planned_mac_to_linux': count(incoming), 'planned_linux_to_mac': count(outgoing),
                  'historical_linux_outbound_excluded': excluded,
                  'ambiguous': {'incoming': ambiguous_in, 'outgoing': ambiguous_out}}
        print(json.dumps(report), flush=True)
        if args.apply:
            details = {}
            for source, dest, files in [('mac', 'linux', incoming), ('linux', 'mac', outgoing)]:
                result = transfer(source, dest, run, files)
                details[source + '_to_' + dest] = result
                report[source + '_to_' + dest] = {'applied': count(result['applied']),
                    'deferred': dict(Counter(i['reason'] for i in result['skipped']))}
                print(json.dumps({source + '_to_' + dest: report[source + '_to_' + dest]}), flush=True)
            (state / ('details-' + run + '.json')).write_text(json.dumps(details, indent=2))
            # Independent fresh inventories verify every file installed by this run.
            for side in ('linux', 'mac'):
                if not any(details[d]['applied'] for d in details if d.endswith('_to_' + side)):
                    continue
                observed = call(side, {'op': 'inventory'})
                planned = incoming if side == 'linux' else outgoing
                expectations = {(i['agent'], i['destination']): i['sha256'] for i in planned}
                for direction, result in details.items():
                    if direction.endswith('_to_' + side):
                        for item in result['applied']:
                            actual = observed[item['agent']].get(item['path'], {}).get('sha256')
                            if actual != expectations[(item['agent'], item['path'])]:
                                raise RuntimeError('Independent verification failed on ' + side)
                new_codex = {item['id'] for direction, result in details.items()
                             if direction.endswith('_to_' + side)
                             for item in result['applied'] if item['agent'] == 'codex'}
                if new_codex:
                    catalog_result = call(side, {'op': 'catalog', 'verify_ids': sorted(new_codex)})
                    missing = new_codex - set(catalog_result.get('ids', []))
                    report[side + '_catalog'] = {'imported_visible': len(new_codex) - len(missing),
                                                'not_listed': len(missing),
                                                'readable': len(new_codex & set(catalog_result.get('readable_ids', []))),
                                                'error': catalog_result.get('error')}
            report['verified'] = True
        path = state / ('report-' + run + '.json')
        path.write_text(json.dumps(report, indent=2)); path.chmod(0o600)
        (state / 'latest-report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'complete': True, 'verified': report.get('verified', False)}))


if __name__ == '__main__':
    main()
