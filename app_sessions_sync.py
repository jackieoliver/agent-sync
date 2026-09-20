#!/usr/bin/env python3
"""Two-way sync of the Claude desktop app's session sidebar records (local_*.json).

The app lists sessions from its own store, not from ~/.claude/projects, so synced
transcripts are invisible until a record exists here too. For each record whose
working folder exists on the other machine (after the home / GitHub prefix swap),
this writes a rewritten record there and moves the already-synced transcript under
the matching project folder so the app can open and resume it.

Rules: never delete; overwrite a record only when the source is newer; back up
anything replaced under state/app-sessions-backups; pre-setup Linux sessions never
go to the Mac; scratch-workspace sessions and folders missing on the other side
are skipped and reported. Records changed in the last 60 s are left alone.
Single file, runs on both sides (--inventory / --receive on the Mac). Python 3.9 ok.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time

BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
from syncconf import CONF
REMOTE = CONF['mac_remote_dir']
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
       '-o', 'UserKnownHostsFile=' + str(BASE / 'mac-known-hosts')]
SETTLE_SECONDS = 60
DROP_FIELDS = ('error', 'errorCategory', 'errorAt', 'priorErrorMark')

SIDES = {k: {'home': CONF[k]['home'], 'github': CONF[k]['github'], 'store': CONF[k]['app_store']}
         for k in ('linux', 'mac')}
SCRATCH = 'scratch-workspaces'


def side():
    return 'mac' if sys.platform == 'darwin' else 'linux'


def norm(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def enc(path):
    return re.sub(r'[^A-Za-z0-9]', '-', path)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def projects_dir():
    return Path.home() / '.claude' / 'projects'


def inventory():
    me = SIDES[side()]
    store = Path(me['store'])
    out = {'records': {}, 'repos': {}, 'transcripts': {}, 'now': time.time()}
    if store.is_dir():
        for p in store.rglob('local_*.json'):
            try:
                d = json.loads(p.read_bytes())
            except Exception:
                continue
            rel = p.relative_to(store).as_posix()
            out['records'][rel] = {
                'sessionId': d.get('sessionId'), 'cliSessionId': d.get('cliSessionId'),
                'cwd': d.get('cwd'), 'title': d.get('title'), 'isArchived': d.get('isArchived'),
                'lastActivityAt': d.get('lastActivityAt') or 0, 'lastFocusedAt': d.get('lastFocusedAt') or 0,
                'mtime': p.stat().st_mtime, 'sha': sha256_bytes(p.read_bytes()),
            }
    gh = Path(me['github'])
    if gh.is_dir():
        out['repos'] = {norm(d.name): d.name for d in gh.iterdir() if d.is_dir()}
    root = projects_dir()
    if root.is_dir():
        for p in root.glob('*/*.jsonl'):
            out['transcripts'].setdefault(p.stem, []).append(p.parent.name)
    return out


def map_cwd(cwd, src, dst, dst_inv):
    """Translate a working folder to the other machine, or None if it can't map."""
    s, d = SIDES[src], SIDES[dst]
    if not cwd or SCRATCH in cwd:
        return None
    if cwd == s['github']:
        return d['github']
    if cwd.startswith(s['github'] + '/'):
        rest = cwd[len(s['github']) + 1:].split('/')
        real = dst_inv['repos'].get(norm(rest[0]))
        if not real:
            return None
        return '/'.join([d['github'], real] + rest[1:])
    if cwd == s['home']:
        return d['home']
    if cwd.startswith(s['home'] + '/'):
        return d['home'] + cwd[len(s['home']):]
    return None


def plan(src_inv, dst_inv, src, dst, excluded_ids):
    """Records to ship src->dst: [(rel, mapped_cwd, expected_dst_sha)] plus skip reasons."""
    items, skipped = [], {}
    by_rel = dst_inv['records']
    for rel, r in src_inv['records'].items():
        cid = r['cliSessionId']
        if not cid:
            skipped[rel] = 'no cli session id'
            continue
        if cid in excluded_ids:
            skipped[rel] = 'pre-setup history stays local'
            continue
        if src_inv['now'] - r['mtime'] < SETTLE_SECONDS:
            skipped[rel] = 'changed in the last minute'
            continue
        mapped = map_cwd(r['cwd'], src, dst, dst_inv)
        if not mapped:
            skipped[rel] = 'folder has no counterpart: ' + str(r['cwd'])
            continue
        if cid not in dst_inv['transcripts']:
            skipped[rel] = 'transcript not synced yet'
            continue
        existing = by_rel.get(rel)
        if existing:
            if existing['lastActivityAt'] >= r['lastActivityAt'] and existing['cwd'] == mapped:
                continue  # destination is current
            items.append((rel, mapped, existing['sha']))
        else:
            items.append((rel, mapped, None))
    return items, skipped


def pack(items):
    me = SIDES[side()]
    buf = io.BytesIO()
    manifest = []
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for i, (rel, mapped, expected) in enumerate(items):
            p = Path(me['store']) / rel
            if not p.is_file():
                continue
            d = json.loads(p.read_bytes())
            d['cwd'] = d['originCwd'] = mapped
            for k in DROP_FIELDS:
                d.pop(k, None)
            data = json.dumps(d).encode()
            info = tarfile.TarInfo('r%d' % i)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            manifest.append({'name': 'r%d' % i, 'rel': rel, 'cwd': mapped, 'expected': expected,
                             'cliSessionId': d.get('cliSessionId'), 'title': d.get('title')})
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def place_transcript(cid, cwd, backup_dir):
    """Ensure projects/<enc(cwd)>/<cid>.jsonl exists, moving a synced copy from another project dir."""
    root = projects_dir()
    target_dir = root / enc(cwd)
    target = target_dir / (cid + '.jsonl')
    if target.is_file():
        return 'present'
    sources = [p for p in root.glob('*/' + cid + '.jsonl') if p.parent != target_dir]
    if not sources:
        return None
    src = max(sources, key=lambda p: p.stat().st_size)
    target_dir.mkdir(parents=True, exist_ok=True)
    os.rename(src, target)
    side_dir = src.parent / cid
    if side_dir.is_dir() and not (target_dir / cid).exists():
        os.rename(side_dir, target_dir / cid)
    (Path(backup_dir) / 'moved-transcripts.log').parent.mkdir(parents=True, exist_ok=True)
    with (Path(backup_dir) / 'moved-transcripts.log').open('a') as fh:
        fh.write('%s -> %s\n' % (src, target))
    return 'moved'


def unpack(blob, backup_dir):
    me = SIDES[side()]
    store = Path(me['store'])
    applied, skipped = [], []
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tar:
        manifest = json.load(tar.extractfile('manifest.json'))
        for m in manifest:
            rel = Path(m['rel'])
            if rel.is_absolute() or '..' in rel.parts or not rel.name.startswith('local_'):
                skipped.append((m['rel'], 'unsafe path'))
                continue
            if not Path(m['cwd']).is_dir():
                skipped.append((m['rel'], 'folder missing: ' + m['cwd']))
                continue
            placed = place_transcript(m['cliSessionId'], m['cwd'], backup_dir)
            if not placed:
                skipped.append((m['rel'], 'transcript not synced yet'))
                continue
            dst = store / rel
            if dst.exists():
                cur = dst.read_bytes()
                if m['expected'] is None or sha256_bytes(cur) != m['expected']:
                    skipped.append((m['rel'], 'destination record changed since plan'))
                    continue
                bk = Path(backup_dir) / rel
                bk.parent.mkdir(parents=True, exist_ok=True)
                bk.write_bytes(cur)
            elif m['expected'] is not None:
                skipped.append((m['rel'], 'destination record vanished since plan'))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(m['name']).read()
            tmp = dst.with_name(dst.name + '.tmp-sync')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dst)
            applied.append((m['rel'], m['title'], placed))
    return {'applied': applied, 'skipped': skipped}


# ---------- Linux driver ----------

def remote(mac, args, stdin=b''):
    cmd = SSH + [mac, shlex.join(['/usr/bin/python3', REMOTE + '/app_sessions_sync.py'] + args)]
    p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=600)
    if p.returncode:
        raise RuntimeError('mac endpoint failed: ' + p.stderr.decode(errors='replace')[-1500:])
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--inventory', action='store_true')
    ap.add_argument('--receive', action='store_true')
    a = ap.parse_args()
    if a.inventory:
        print(json.dumps(inventory()))
        return
    if a.receive:
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        print(json.dumps(unpack(sys.stdin.buffer.read(), STATE / 'app-sessions-backups' / stamp)))
        return

    import mac_host
    from sync_sessions import historical_ids
    mac = mac_host.target()
    STATE.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(['scp', '-q'] + SSH[1:] + [str(BASE / 'app_sessions_sync.py'), str(BASE / 'syncconf.py'), str(BASE / 'config.json'), mac + ':' + REMOTE + '/'],
                       capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError('scp to mac failed: ' + (p.stderr.strip() or 'exit ' + str(p.returncode)))
    linux = inventory()
    macinv = json.loads(remote(mac, ['--inventory']))
    to_linux, skip_ml = plan(macinv, linux, 'mac', 'linux', set())
    to_mac, skip_lm = plan(linux, macinv, 'linux', 'mac', historical_ids()['claude'])
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    report = {'run': stamp, 'mode': 'apply' if a.apply else 'preview',
              'planned_mac_to_linux': [(r, c) for r, c, _ in to_linux],
              'planned_linux_to_mac': [(r, c) for r, c, _ in to_mac],
              'skipped_mac_to_linux': skip_ml, 'skipped_linux_to_mac': skip_lm}
    if a.apply:
        if to_linux:
            # The Mac packs its own records; they are unpacked here.
            blob = remote(mac, ['--pack'], json.dumps(to_linux).encode())
            report['mac_to_linux'] = unpack(blob, STATE / 'app-sessions-backups' / stamp)
        if to_mac:
            report['linux_to_mac'] = json.loads(remote(mac, ['--receive'], pack(to_mac)))
        report['verified'] = True
    (STATE / 'app-sessions-latest.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    if '--pack' in sys.argv:
        items = [tuple(x) for x in json.load(sys.stdin)]
        sys.stdout.buffer.write(pack(items))
    else:
        main()
