#!/usr/bin/env python3
"""systemd entry point: Claude Code memory sync between the Mac and Linux.

Conversation and sidebar-record sync were retired on 2026-10-08 (they kept
diverging; each machine now keeps its own chats and the Mac's are in the nightly
restic backup). check_sync_health.py and app_sessions_sync.py remain for reference.

Alerts go to the desktop via notify-send, deduplicated the same way the Codex
automation did: one alert per unresolved incident, offline is never an alert,
recovery is silent. Everything is appended to state/scheduler.log.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
LOG = STATE / 'scheduler.log'
MEM_HEALTH = STATE / 'memory-health.json'
NETWORK = ('network is unreachable', 'no route to host', 'timed out', 'connection refused',
           'connection reset', 'connection closed by', 'broken pipe', 'could not resolve hostname',
           'temporary failure in name resolution', 'connection unexpectedly closed',
           'exit status 255', 'connection closed', 'scp to mac failed: exit 255')


def log(msg):
    with LOG.open('a') as fh:
        fh.write(time.strftime('%Y-%m-%dT%H:%M:%S%z') + ' ' + msg + '\n')


def notify(title, body):
    log('ALERT ' + title + ': ' + body)
    try:
        subprocess.run(['notify-send', '-u', 'critical', '-a', 'agent-sync', title, body], timeout=10)
    except Exception as e:  # desktop bus may be absent; the log still has it
        log('notify-send failed: ' + str(e))


ALERT = STATE / 'alert-pending.json'
STALE_SECONDS = 2 * 3600   # Mac reachable, yet nothing has succeeded for this long -> broken


def mac_reachable():
    import socket
    from syncconf import CONF
    for host in CONF['mac_hosts']:
        try:
            with socket.create_connection((host, 22), timeout=3):
                return True
        except OSError:
            continue
    return False


def raise_alert(kind, message):
    """Record an incident for the email escalation; one file per unresolved incident."""
    cur = json.loads(ALERT.read_text()) if ALERT.exists() else None
    if cur and cur.get('kind') == kind:
        return
    ALERT.write_text(json.dumps({'kind': kind, 'message': message, 'since': time.time(),
                                 'host': os.uname().nodename, 'emailed': False}, indent=2))
    notify('Sync needs attention', message)


def clear_alert():
    if ALERT.exists():
        ALERT.unlink()
        log('incident cleared')


def run(args, timeout):
    p = subprocess.run([sys.executable] + args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    # memories
    mh = json.loads(MEM_HEALTH.read_text()) if MEM_HEALTH.exists() else {}
    try:
        rc, out, err = run([str(BASE / 'memory_sync.py'), '--apply'], 700)
    except subprocess.TimeoutExpired:
        rc, out, err = 124, '', 'memory sync exceeded its time limit'
    if rc == 0:
        rep = json.loads(out)
        moved = len(rep.get('linux_to_mac', {}).get('applied', [])) + len(rep.get('mac_to_linux', {}).get('applied', []))
        log('memories: ok, %d file(s) moved, %d conflict(s), %d unmapped' % (moved, len(rep['conflicts']), len(rep['unmapped'])))
        mh.update(status='healthy', consecutive_failures=0, last_success=time.time(), message='ok')
        if rep['conflicts'] and mh.get('alerted_conflicts') != rep['conflicts']:
            raise_alert('memories:conflict', 'Memory sync conflict, same-time edits left untouched: ' + ', '.join(rep['conflicts']))
            mh['alerted_conflicts'] = rep['conflicts']
        elif not rep['conflicts']:
            mh.pop('alerted_conflicts', None)
        if mh.get('alert_active') and mh.get('consecutive_successes', 0) >= 1:
            mh['alert_active'] = False
        mh['consecutive_successes'] = mh.get('consecutive_successes', 0) + 1
    else:
        low = err.lower()
        if any(n in low for n in NETWORK):
            mh.update(status='offline', message='waiting for connection', consecutive_successes=0)
            log('memories: waiting for connection')
        else:
            mh.update(status='failure', message=err.strip()[-800:], consecutive_successes=0,
                      consecutive_failures=mh.get('consecutive_failures', 0) + 1)
            log('memories: FAILED (%d in a row): %s' % (mh['consecutive_failures'], err.strip()[-300:]))
            if mh['consecutive_failures'] >= 3 and not mh.get('alert_active'):
                raise_alert('memories:failure', 'Memory sync failing: ' + err.strip()[-300:])
                mh['alert_active'] = True
    mh['last_check'] = time.time()
    MEM_HEALTH.write_text(json.dumps(mh, indent=2))

    # "online but not working": Mac answers on port 22, yet no success for 2 h
    now = time.time()
    mem_ok = mh.get('last_success') or 0
    if mh.get('status') == 'healthy' and not mh.get('alert_active'):
        clear_alert()
    elif mac_reachable() and now - mem_ok > STALE_SECONDS:
        raise_alert('stale', 'Mac is reachable but memory sync has not succeeded for %.0f min. Last state: %s'
                    % ((now - mem_ok) / 60, mh.get('status')))

if __name__ == '__main__':
    main()
