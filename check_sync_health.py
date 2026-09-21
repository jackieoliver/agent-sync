#!/usr/bin/env python3
"""Run the existing sync and produce a durable, deduplicated alert decision."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
BENIGN = {'active destination', 'open conversation', 'recently active or absent', 'housekeeping only',
          'changed during snapshot', 'destination changed since preview',
          'destination became active', 'destination appeared', 'destination already ahead'}
NETWORK_ERRORS = ('network is unreachable', 'no route to host', 'connection timed out',
                  'operation timed out', 'connection refused', 'connection reset',
                  'connection closed by', 'broken pipe', 'temporary failure in name resolution',
                  'could not resolve hostname', 'connection unexpectedly closed')
ACCESS_ERRORS = ('permission denied', 'host key verification failed',
                 'remote host identification has changed')


def assess(returncode, stdout, stderr, report, previous_run):
    if returncode == 0 and 'Another synchronization is already running.' in stdout:
        return 'busy', 'Another sync is running.'
    if returncode != 0:
        if 'No space left on device' in stderr:
            return 'storage_full', 'Sync failed because a device ran out of storage.'
        diagnostic = stderr.lower()
        # Access errors require action even if a transport error appears as well.
        if any(error in diagnostic for error in ACCESS_ERRORS):
            return 'failure', 'Sync access failed. Check SSH permissions or the Mac host identity.'
        if any(error in diagnostic for error in NETWORK_ERRORS):
            return 'offline', 'Waiting for connection. The Mac, network, or Tailscale is unavailable; retrying automatically.'
        return 'failure', 'Sync failed: ' + (stderr.strip()[-800:] or 'exit code ' + str(returncode))
    if not report or report.get('run') == previous_run or not report.get('verified') or report.get('mode') != 'apply':
        return 'failure', 'Sync exited without a fresh, verified completion report.'
    problems = []
    for direction in ('mac_to_linux', 'linux_to_mac'):
        for reason, count in report.get(direction, {}).get('deferred', {}).items():
            if count and reason not in BENIGN:
                problems.append(f'{direction}: {count} {reason}')
    for direction, counts in report.get('ambiguous', {}).items():
        if any(counts.values()):
            problems.append(f'{direction}: ambiguous conversation identities')
    for side in ('linux', 'mac'):
        catalog = report.get(side + '_catalog', {})
        if catalog.get('error'):
            problems.append(side + ': Codex catalog check failed')
    if problems:
        return 'conflict', '; '.join(problems)
    return 'healthy', 'Sync completed and file verification passed.'


def transition(old, status, message, now):
    state = dict(old)
    state.update(last_check=now, status=status, message=message, notify=False)
    if status == 'busy':
        return state
    if status == 'offline':
        state.update(consecutive_failures=0, consecutive_successes=0)
        # An outage neither creates an incident nor clears a known unresolved one.
        state.setdefault('offline_since', now)
        return state
    state.pop('offline_since', None)
    failed = status in ('failure', 'storage_full')
    state['consecutive_failures'] = old.get('consecutive_failures', 0) + 1 if failed else 0
    if status == 'healthy':
        state['last_success'] = now
        state['consecutive_successes'] = old.get('consecutive_successes', 0) + 1
        if old.get('alert_active') and state['consecutive_successes'] >= 2:
            state['alert_active'] = False
            state['alert_kind'] = None
            state['message'] = 'Conversation sync has recovered: two consecutive checks completed successfully.'
        return state
    state['consecutive_successes'] = 0
    if status == 'conflict':
        # The run completed; only the named files were left alone. Don't let a standing
        # conflict look like the sync stopped working.
        state['last_success'] = now
    eligible = status in ('conflict', 'storage_full') or state['consecutive_failures'] >= 3
    if eligible and not old.get('alert_active'):
        state.update(notify=True, alert_active=True, alert_kind=status, last_alert=now)
    return state


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path, data):
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'health-check.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'busy', 'notify': False}))
            return
        health_path = STATE / 'sync-health.json'
        old = read_json(health_path)
        report_path = STATE / 'latest-report.json'
        previous = read_json(report_path).get('run')
        process = subprocess.Popen([sys.executable, str(BASE / 'sync_sessions.py'), '--apply'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=900)
            rc = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            rc, stderr = 124, 'Sync exceeded its 15-minute execution limit.\n' + stderr
        report = read_json(report_path)
        status, message = assess(rc, stdout, stderr, report, previous)
        health = transition(old, status, message, time.time())
        health['last_report_run'] = report.get('run')
        save_json(health_path, health)
        print(json.dumps(health))


if __name__ == '__main__':
    main()
