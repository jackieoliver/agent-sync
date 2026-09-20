#!/usr/bin/env python3
"""Two-way sync of Claude Code memory folders between Linux and Mac.

Scope: ~/.claude/projects/<project>/memory/** only. Newest file wins, nothing is
ever deleted, and every overwritten file is backed up under state/memory-backups.
Projects are paired by normalized repo name (case/underscore-insensitive) across
the two machines' different GitHub roots. Scratch workspaces are ignored.

This single file runs on both sides: Linux drives it; the Mac copy is refreshed
each run and invoked over SSH with --inventory / --send / --receive.
Python 3.9 compatible (the Mac's system python).
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
MAC = None  # resolved in main(): Tailscale, else Ethernet
from syncconf import CONF
REMOTE = CONF['mac_remote_dir']
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
       '-o', 'UserKnownHostsFile=' + str(BASE / 'mac-known-hosts')]
SETTLE_SECONDS = 60          # skip files written within the last minute (still being edited)
MTIME_TIE_SECONDS = 2        # within this window, treat as a tie -> leave both, report

def _enc(path):
    return re.sub(r'[^A-Za-z0-9]', '-', path)


SIDES = {k: {'home': _enc(CONF[k]['home']), 'github': _enc(CONF[k]['github']), 'repos': Path(CONF[k]['github'])}
         for k in ('linux', 'mac')}
IGNORE = ('scratch-workspaces',)


def norm(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def projects_dir():
    return Path.home() / '.claude' / 'projects'


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def inventory():
    """{project_enc: {rel: {size, mtime, sha}}} for every memory folder on this machine."""
    out = {'projects': {}, 'repos': {}, 'now': time.time()}
    root = projects_dir()
    if root.is_dir():
        for proj in sorted(root.iterdir()):
            if not proj.is_dir() or any(x in proj.name for x in IGNORE):
                continue
            mem = proj / 'memory'
            entry = {}
            if mem.is_dir():
                for p in sorted(mem.rglob('*')):
                    if p.is_file() and not p.is_symlink():
                        st = p.stat()
                        rel = p.relative_to(mem).as_posix()
                        entry[rel] = {'size': st.st_size, 'mtime': st.st_mtime, 'sha': sha256(p)}
                        if rel == 'MEMORY.md' and st.st_size < 200000:
                            entry[rel]['text'] = p.read_text(errors='replace')
            out['projects'][proj.name] = entry
    side = 'mac' if sys.platform == 'darwin' else 'linux'
    repos = SIDES[side]['repos']
    if repos.is_dir():
        out['repos'] = {norm(d.name): d.name for d in repos.iterdir() if d.is_dir()}
    out['home_dirs'] = {norm(d.name): d.name for d in Path.home().iterdir() if d.is_dir() and not d.name.startswith('.')}
    return out


def counterpart(enc, src, dst, dst_inv):
    """Map a project folder name from one machine to the other, or None."""
    s, d = SIDES[src], SIDES[dst]
    if enc == s['home']:
        return d['home']
    if enc == s['github']:
        return d['github']
    if enc.startswith(s['github'] + '-'):
        remainder = enc[len(s['github']) + 1:]
        want = norm(remainder)
        # 1. an existing project dir on the destination with the same normalized repo name
        for cand in dst_inv['projects']:
            if cand.startswith(d['github'] + '-') and norm(cand[len(d['github']) + 1:]) == want:
                return cand
        # 2. the repo exists on disk there: derive the encoded name Claude Code would use
        real = dst_inv['repos'].get(want)
        if real:
            return d['github'] + '-' + re.sub(r'[^A-Za-z0-9]', '-', real)
    if enc.startswith(s['home'] + '-'):
        # a top-level home folder such as Downloads, present on both machines
        want = norm(enc[len(s['home']) + 1:])
        real = dst_inv.get('home_dirs', {}).get(want)
        if real and '-' not in real:
            return d['home'] + '-' + re.sub(r'[^A-Za-z0-9]', '-', real)
    return None


INDEX_LINK = re.compile(r'\]\(([^)]+)\)')


def merge_index(newer, older):
    """Union of two MEMORY.md indexes: newer's lines in order, then older's lines it lacks."""
    def key(line):
        m = INDEX_LINK.search(line)
        return ('link', m.group(1).strip()) if m else ('line', line.strip())
    out = newer.rstrip('\n').split('\n') if newer.strip() else []
    seen = {key(l) for l in out if l.strip()}
    extra = [l for l in older.split('\n') if l.strip() and key(l) not in seen]
    if extra:
        out.extend(extra)
    return '\n'.join(out).rstrip('\n') + '\n'


def plan(linux, mac):
    """Return (to_mac, to_linux, conflicts, unmapped, merges).
    Items: (src_proj, rel, dst_proj, expected_dst_sha). merges: (linux_proj, mac_proj, merged_text, linux_sha, mac_sha)."""
    to_mac, to_linux, conflicts, unmapped, merges = [], [], [], [], []
    pairs = {}
    for enc in linux['projects']:
        m = counterpart(enc, 'linux', 'mac', mac)
        if m:
            pairs[(enc, m)] = True
        elif linux['projects'][enc]:
            unmapped.append('linux:' + enc)
    for enc in mac['projects']:
        l = counterpart(enc, 'mac', 'linux', linux)
        if l:
            pairs[(l, enc)] = True
        elif mac['projects'][enc]:
            unmapped.append('mac:' + enc)
    now = max(linux['now'], mac['now'])
    for lp, mp in sorted(pairs):
        lf = linux['projects'].get(lp, {})
        mf = mac['projects'].get(mp, {})
        for rel in sorted(set(lf) | set(mf)):
            a, b = lf.get(rel), mf.get(rel)
            if a and b and a['sha'] == b['sha']:
                continue
            if (a and now - a['mtime'] < SETTLE_SECONDS) or (b and now - b['mtime'] < SETTLE_SECONDS):
                continue
            if a and b and rel == 'MEMORY.md' and 'text' in a and 'text' in b:
                newer, older = (a, b) if a['mtime'] >= b['mtime'] else (b, a)
                merges.append((lp, mp, merge_index(newer['text'], older['text']), a['sha'], b['sha']))
                continue
            if a and not b:
                to_mac.append((lp, rel, mp, None))
            elif b and not a:
                to_linux.append((mp, rel, lp, None))
            elif abs(a['mtime'] - b['mtime']) <= MTIME_TIE_SECONDS:
                conflicts.append(lp + '/memory/' + rel)
            elif a['mtime'] > b['mtime']:
                to_mac.append((lp, rel, mp, b['sha']))
            else:
                to_linux.append((mp, rel, lp, a['sha']))
    return to_mac, to_linux, conflicts, unmapped, merges


def pack(items):
    """items: [(src_proj, rel, dst_proj, expected)] -> tar bytes with a manifest."""
    buf = io.BytesIO()
    root = projects_dir()
    manifest = []
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for i, (sp, rel, dp, expected) in enumerate(items):
            src = root / sp / 'memory' / rel
            if not src.is_file():
                continue
            name = 'f%d' % i
            tar.add(src, arcname=name, recursive=False)
            manifest.append({'name': name, 'dst_proj': dp, 'rel': rel, 'expected': expected,
                             'mtime': src.stat().st_mtime})
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(blob, backup_dir):
    """Write files from a pack() tar into this machine's memory folders. Returns a report."""
    root = projects_dir()
    applied, skipped = [], []
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tar:
        manifest = json.load(tar.extractfile('manifest.json'))
        for m in manifest:
            rel = Path(m['rel'])
            if rel.is_absolute() or '..' in rel.parts or '/' in m['dst_proj'] or m['dst_proj'] in ('.', '..'):
                skipped.append((m['dst_proj'], m['rel'], 'unsafe path'))
                continue
            dst = root / m['dst_proj'] / 'memory' / rel
            if dst.exists():
                if m['expected'] is None or sha256(dst) != m['expected']:
                    skipped.append((m['dst_proj'], m['rel'], 'destination changed since plan'))
                    continue
                bk = Path(backup_dir) / m['dst_proj'] / 'memory' / rel
                bk.parent.mkdir(parents=True, exist_ok=True)
                bk.write_bytes(dst.read_bytes())
                os.utime(bk, (dst.stat().st_atime, dst.stat().st_mtime))
            elif m['expected'] is not None:
                skipped.append((m['dst_proj'], m['rel'], 'destination vanished since plan'))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(m['name']).read()
            tmp = dst.with_name(dst.name + '.tmp-sync')
            with open(tmp, 'wb') as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.utime(tmp, (m['mtime'], m['mtime']))
            os.replace(tmp, dst)
            applied.append((m['dst_proj'], m['rel']))
    return {'applied': applied, 'skipped': skipped}


# ---------- remote plumbing (Linux side) ----------

def remote(args, stdin=b''):
    cmd = SSH + [MAC, shlex.join(['/usr/bin/python3', REMOTE + '/memory_sync.py'] + args)]
    p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=600)
    if p.returncode:
        raise RuntimeError('mac endpoint failed: ' + p.stderr.decode(errors='replace')[-1500:])
    return p.stdout


def push_script():
    p = subprocess.run(['scp', '-q'] + SSH[1:] + [str(BASE / 'memory_sync.py'), str(BASE / 'syncconf.py'), str(BASE / 'config.json'), MAC + ':' + REMOTE + '/'],
                       capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError('scp to mac failed: ' + (p.stderr.strip() or 'exit ' + str(p.returncode)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--inventory', action='store_true', help='(remote) print inventory JSON')
    ap.add_argument('--send', action='store_true', help='(remote) read item list JSON on stdin, write tar to stdout')
    ap.add_argument('--receive', action='store_true', help='(remote) read tar on stdin, apply, print report JSON')
    a = ap.parse_args()

    if a.inventory:
        print(json.dumps(inventory()))
        return
    if a.send:
        items = [tuple(x) for x in json.load(sys.stdin)]
        sys.stdout.buffer.write(pack(items))
        return
    if a.receive:
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        rep = unpack(sys.stdin.buffer.read(), STATE / 'memory-backups' / stamp)
        print(json.dumps(rep))
        return

    global MAC
    import mac_host
    MAC = mac_host.target()
    STATE.mkdir(parents=True, exist_ok=True)
    push_script()
    linux = inventory()
    mac = json.loads(remote(['--inventory']))
    to_mac, to_linux, conflicts, unmapped, merges = plan(linux, mac)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    report = {'run': stamp, 'mode': 'apply' if a.apply else 'preview',
              'planned_linux_to_mac': [p + '/memory/' + r for p, r, _, _ in to_mac],
              'planned_mac_to_linux': [p + '/memory/' + r for p, r, _, _ in to_linux],
              'conflicts': conflicts, 'unmapped': unmapped,
              'planned_index_merges': [lp + '/memory/MEMORY.md' for lp, _, _, _, _ in merges]}
    if a.apply:
        root = projects_dir()
        for lp, mp, text, lsha, msha in merges:
            dst = root / lp / 'memory' / 'MEMORY.md'
            if sha256(dst) != lsha:
                continue  # changed since plan; next run
            bk = STATE / 'memory-backups' / stamp / lp / 'memory' / 'MEMORY.md'
            bk.parent.mkdir(parents=True, exist_ok=True)
            bk.write_bytes(dst.read_bytes())
            tmp = dst.with_name(dst.name + '.tmp-sync')
            tmp.write_text(text)
            os.replace(tmp, dst)
            to_mac.append((lp, 'MEMORY.md', mp, msha))
        if to_mac:
            report['linux_to_mac'] = json.loads(remote(['--receive'], pack(to_mac)))
        if to_linux:
            blob = remote(['--send'], json.dumps(to_linux).encode())
            report['mac_to_linux'] = unpack(blob, STATE / 'memory-backups' / stamp)
        report['verified'] = True
    (STATE / 'memory-latest.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
