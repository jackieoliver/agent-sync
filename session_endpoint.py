#!/usr/bin/env python3
"""Conservative conversation-file transfer endpoint; no credentials or databases."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import selectors

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
QUIET_SECONDS = 120


def identity(agent, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('unsafe relative path')
    if agent == 'codex':
        if len(p.parts) < 2 or p.parts[0] not in ('sessions', 'archived_sessions') or p.suffix != '.jsonl':
            raise ValueError('unsupported Codex file')
        matches = UUID.findall(p.name)
        if not matches:
            raise ValueError('missing session ID')
        return matches[-1]
    if agent == 'claude':
        if len(p.parts) < 3 or p.parts[0] != 'projects':
            raise ValueError('unsupported Claude file')
        top = p.parts[2]
        if len(p.parts) == 3 and p.suffix == '.jsonl' and UUID.fullmatch(p.stem):
            return p.stem
        if len(p.parts) > 3 and UUID.fullmatch(top):
            return top
        raise ValueError('not a conversation or its attachments')
    raise ValueError('unsupported agent')


def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def stamp(p):
    s = p.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ino]


def confined(root, relative):
    p = root / relative
    if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('symlink or escaping path')
    return p


def file_open(p):
    exe = shutil.which('lsof') or '/usr/sbin/lsof'
    r = subprocess.run([exe, '-t', '--', str(p)], capture_output=True, timeout=10)
    if r.returncode not in (0, 1):
        raise RuntimeError('Cannot determine whether conversation is open')
    return bool(r.stdout.strip())


@contextlib.contextmanager
def codex_guard(root, session_id):
    directory = root / 'thread-writer-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / '.coordination.lock').open('a+b') as coordination:
        fcntl.flock(coordination, fcntl.LOCK_EX)
        # Match Codex's per-thread lock filename (confirmed during setup).
        with (directory / (session_id + '.lock')).open('a+b') as thread:
            try:
                fcntl.flock(thread, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(thread, fcntl.LOCK_UN)


def inventory(home, state):
    cache_path = state / 'hash-cache.json'
    try:
        cache = json.loads(cache_path.read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    updated, result = {}, {}
    for agent in ('codex', 'claude'):
        root = home / ('.' + agent)
        result[agent] = {}
        bases = [root / x for x in ('sessions', 'archived_sessions')] if agent == 'codex' else [root / 'projects']
        for base in bases:
            if not base.exists():
                continue
            for p in base.rglob('*'):
                if not p.is_file() or p.is_symlink():
                    continue
                rel = p.relative_to(root).as_posix()
                try:
                    sid = identity(agent, rel)
                    confined(root, rel)
                except ValueError:
                    continue
                before = stamp(p)
                key = agent + '/' + rel
                old = cache.get(key, {})
                sha = old.get('sha256') if old.get('stamp') == before else digest(p)
                if stamp(p) != before:
                    continue
                updated[key] = {'stamp': before, 'sha256': sha}
                result[agent][rel] = {'id': sid, 'sha256': sha, 'size': before[0], 'mtime_ns': before[1]}
    atomic_json(cache_path, updated)
    return result


def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data))
    temp.chmod(0o600)
    os.replace(temp, path)


def prepare(home, state, request):
    # Copies only stable, closed files into a private outgoing staging area.
    stage = state / 'outgoing' / request['run']
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepared, skipped = [], []
    for item in request['files']:
        agent, rel = item['agent'], item['path']
        sid = identity(agent, rel)
        root = home / ('.' + agent)
        p = confined(root, rel)
        if not p.exists() or time.time_ns() - p.stat().st_mtime_ns < QUIET_SECONDS * 10**9:
            skipped.append({'agent': agent, 'path': rel, 'reason': 'recently active or absent'})
            continue
        guard = codex_guard(root, sid) if agent == 'codex' else contextlib.nullcontext(True)
        with guard as available:
            if not available or file_open(p):
                skipped.append({'agent': agent, 'path': rel, 'reason': 'open conversation'})
                continue
            before = stamp(p)
            target = confined(stage / agent, rel)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(p, target)
            target.chmod(0o600)
            sha = digest(target)
            if stamp(p) != before or sha != item['sha256']:
                target.unlink()
                skipped.append({'agent': agent, 'path': rel, 'reason': 'changed during snapshot'})
                continue
            prepared.append(item)
    return {'files': prepared, 'skipped': skipped}


def valid_jsonl(path):
    with path.open('rb') as f:
        for line in f:
            if line.strip():
                if not line.endswith(b'\n'):
                    raise ValueError('incomplete final record')
                json.loads(line)


def prefix_of(a, b):
    if a.stat().st_size > b.stat().st_size:
        return False
    with a.open('rb') as fa, b.open('rb') as fb:
        while True:
            block = fa.read(1024 * 1024)
            if not block:
                return True
            if fb.read(len(block)) != block:
                return False


HOUSEKEEPING = (b'"type":"bridge-session"', b'"type": "bridge-session"',
                b'"type":"artifact-comment-monitor"', b'"type": "artifact-comment-monitor"')


def content_lines(path):
    """Conversation records only: per-app bookkeeping lines are rewritten by each machine's app."""
    out = []
    with path.open('rb') as f:
        for line in f:
            if any(tag in line for tag in HOUSEKEEPING):
                continue
            out.append(line)
    return out


def compare_content(dest, source):
    """'same' | 'prefix' (dest is a prefix of source) | 'ahead' (source is a prefix of dest) | 'diverged'."""
    a, b = content_lines(dest), content_lines(source)
    n = min(len(a), len(b))
    if a[:n] != b[:n]:
        return 'diverged'
    if len(a) == len(b):
        return 'same'
    return 'prefix' if len(a) < len(b) else 'ahead'


def apply(home, state, request):
    applied, skipped = [], []
    stage = state / 'incoming' / request['run']
    for item in request['files']:
        agent, rel = item['agent'], item['path']
        sid = identity(agent, rel)
        root = home / ('.' + agent)
        source = confined(stage / agent, rel)
        destrel = item.get('destination', rel)
        if identity(agent, destrel) != sid:
            raise ValueError('destination identity mismatch')
        dest = confined(root, destrel)
        if not source.exists() or digest(source) != item['sha256']:
            raise ValueError('staged checksum mismatch')
        if source.suffix == '.jsonl':
            try:
                valid_jsonl(source)
            except (ValueError, UnicodeDecodeError):
                skipped.append({'agent': agent, 'path': rel, 'reason': 'invalid or incomplete JSONL'})
                continue
        guard = codex_guard(root, sid) if agent == 'codex' else contextlib.nullcontext(True)
        with guard as available:
            if not available:
                skipped.append({'agent': agent, 'path': rel, 'reason': 'active destination'})
                continue
            exists = dest.exists()
            previous = digest(dest) if exists else None
            if previous == item['sha256']:
                continue
            if previous != item.get('expected'):
                skipped.append({'agent': agent, 'path': rel, 'reason': 'destination changed since preview'})
                continue
            if exists:
                if time.time_ns() - dest.stat().st_mtime_ns < QUIET_SECONDS * 10**9 or file_open(dest):
                    skipped.append({'agent': agent, 'path': rel, 'reason': 'active destination'})
                    continue
                # Never choose a winner for diverged histories or overwrite compacted history.
                if not prefix_of(dest, source):
                    if source.suffix == '.jsonl':
                        relation = compare_content(dest, source)
                    else:
                        relation = 'ahead' if prefix_of(source, dest) else 'diverged'
                    if relation == 'same':
                        skipped.append({'agent': agent, 'path': rel, 'reason': 'housekeeping only'})
                        continue
                    if relation != 'prefix':
                        reason = 'destination already ahead' if relation == 'ahead' else 'diverged histories'
                        skipped.append({'agent': agent, 'path': rel, 'reason': reason})
                        continue
                backup = confined(state / 'backups' / request['run'] / agent, destrel)
                backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(dest, backup)
                backup.chmod(0o600)
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.session-sync-', dir=dest.parent)
            os.close(fd)
            temp = Path(name)
            try:
                shutil.copy2(source, temp)
                temp.chmod(0o600)
                if exists:
                    if digest(dest) != previous or file_open(dest):
                        skipped.append({'agent': agent, 'path': rel, 'reason': 'destination became active'})
                        continue
                    os.replace(temp, dest)
                else:
                    # Atomic create without replacing a conversation created since preview.
                    try:
                        os.link(temp, dest)
                    except FileExistsError:
                        skipped.append({'agent': agent, 'path': rel, 'reason': 'destination appeared'})
                        continue
                if digest(dest) != item['sha256']:
                    raise RuntimeError('post-copy verification failed')
                applied.append({'agent': agent, 'path': destrel, 'id': sid})
            finally:
                temp.unlink(missing_ok=True)
    return {'applied': applied, 'skipped': skipped}


def precheck(home, state, request):
    """Report planned destinations that would be refused, so the source never stages them."""
    active = []
    for item in request['files']:
        agent, rel = item['agent'], item['path']
        destrel = item.get('destination', rel)
        try:
            sid = identity(agent, destrel)
            dest = confined(home / ('.' + agent), destrel)
        except ValueError:
            continue
        busy = False
        if agent == 'codex':
            with codex_guard(home / '.codex', sid) as available:
                busy = not available
        if not busy and dest.exists():
            busy = (time.time_ns() - dest.stat().st_mtime_ns < QUIET_SECONDS * 10**9) or file_open(dest)
        if busy:
            active.append({'agent': agent, 'path': rel, 'reason': 'active destination'})
    return {'active': active}


def cleanup(state, request):
    """Delete this run's staging on both directions, plus stale leftovers from crashed runs."""
    removed = []
    for kind in ('outgoing', 'incoming'):
        base = state / kind
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            stale = time.time() - d.stat().st_mtime > request.get('stale_seconds', 2 * 3600)
            if d.name == request.get('run') or stale or request.get('all'):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(kind + '/' + d.name)
    keep_days = request.get('backup_keep_days', 14)
    backups = state / 'backups'
    if backups.is_dir():
        for d in backups.iterdir():
            if d.is_dir() and time.time() - d.stat().st_mtime > keep_days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                removed.append('backups/' + d.name)
    return {'removed': removed}


def catalog(state, verify_ids=()):
    exe = None
    for candidate in ('/usr/lib/chatgpt/resources/codex', '/Applications/ChatGPT.app/Contents/Resources/codex', '/Applications/Codex.app/Contents/Resources/codex'):
        if Path(candidate).exists():
            exe = candidate
            break
    exe = exe or shutil.which('codex')
    if not exe:
        return {'error': 'Codex executable unavailable'}
    child = subprocess.Popen([exe, 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    selector = selectors.DefaultSelector(); selector.register(child.stdout, selectors.EVENT_READ)
    pending = b''
    request_id = 0
    def rpc(method, params):
        nonlocal request_id, pending
        request_id += 1
        child.stdin.write((json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n').encode())
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                value = json.loads(line)
                if value.get('id') == request_id:
                    if 'error' in value:
                        raise RuntimeError('Codex catalog request failed: ' + str(value['error']))
                    return value['result']
            if selector.select(1):
                block = os.read(child.stdout.fileno(), 1024 * 1024)
                if not block:
                    raise RuntimeError('Codex catalog process ended')
                pending += block
        raise TimeoutError('Codex catalog refresh timed out')
    try:
        rpc('initialize', {'clientInfo': {'name': 'filtered-session-sync', 'version': '1.0'}})
        child.stdin.write(b'{"method":"initialized","params":{}}\n')
        ids = set()
        kinds = ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
                 'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']
        for archived in (False, True):
            cursor = None
            while True:
                result = rpc('thread/list', {'limit': 1000, 'cursor': cursor, 'archived': archived,
                             'useStateDbOnly': False, 'sourceKinds': kinds})
                ids.update(t['id'] for t in result['data'])
                cursor = result.get('nextCursor')
                if not cursor:
                    break
        readable, errors = set(ids), {}
        for sid in set(verify_ids) - ids:
            if not UUID.fullmatch(sid):
                raise ValueError('invalid catalog verification ID')
            try:
                result = rpc('thread/read', {'threadId': sid, 'includeTurns': False})
                if result.get('thread', {}).get('id') == sid:
                    readable.add(sid)
            except RuntimeError as error:
                errors[sid] = str(error)
        value = {'ids': sorted(ids), 'readable_ids': sorted(readable), 'read_errors': errors,
                 'count': len(ids), 'updated_at': time.time()}
        atomic_json(state / 'codex-catalog.json', value)
        return value
    finally:
        selector.close()
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.terminate(); child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    parser.add_argument('--home', default=str(Path.home()))
    args = parser.parse_args()
    state, home = Path(args.state), Path(args.home)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    req = json.load(sys.stdin)
    if req.get('run') and not re.fullmatch(r'[0-9TZa-f-]+', req['run']):
        raise ValueError('invalid run ID')
    with (state / 'endpoint.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if req['op'] == 'inventory':
            result = inventory(home, state)
        elif req['op'] == 'prepare':
            result = prepare(home, state, req)
        elif req['op'] == 'apply':
            result = apply(home, state, req)
        elif req['op'] == 'catalog':
            result = catalog(state, req.get('verify_ids', []))
        elif req['op'] == 'precheck':
            result = precheck(home, state, req)
        elif req['op'] == 'cleanup':
            result = cleanup(state, req)
        else:
            raise ValueError('unsupported operation')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
