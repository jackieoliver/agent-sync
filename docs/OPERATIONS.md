# Operations runbook

Machine-specific values (Mac user, addresses, home and GitHub roots, alert email) live in `config.json`, which is gitignored; copy `config.example.json` to start. The Mac needs the same file next to the scripts.

Configured September 18, 2026. Linux cutoff recorded at 2026-09-19T00:25:11.418640+00:00.

## Behavior

- Existing Mac conversation files copy into Linux.
- Conversations present on Linux before setup never upload to the Mac, even if resumed later.
- New conversations sync in both directions. Imported Mac conversations can also sync later edits in either direction.
- Open conversations and files changed within the last two minutes are deferred.
- Existing Codex destination files require byte-prefix compatibility. Claude JSONL can also be compared by ordered UUID-bearing conversation records, ignoring machine-specific housekeeping; matching conversation prefixes can be updated. Diverged conversation content is left untouched for review. Source deletions are not propagated; staging files and older backups have a separate cleanup policy.
- Project files, login credentials, app settings, plugins, memories, and databases are not included. Original project paths inside transcripts are preserved; resuming work on another machine may require selecting an available local project folder.

## Automatic operation

**Since September 19, 2026: a systemd user timer on Linux** (`agent-sync.timer`, every 5 minutes, lingering enabled) runs `scheduled_sync.py`, which runs `check_sync_health.py` (conversations) and then `memory_sync.py --apply` (Claude Code memories). It uses no model tokens. The earlier Codex automation **Sync Mac and Linux conversations** is PAUSED: it re-sent a ~340K-token thread on every run and exhausted the Codex quota. Leave it paused.

The Mac must be awake and reachable. Every script picks the Mac address at run time via `mac_host.py`: the first entry of `mac_hosts` in `config.json` (Tailscale) first, then the second (direct Ethernet); both are pinned in `mac-known-hosts`. Otherwise runs log "waiting for connection" and retry. Alerts arrive as desktop notifications (`notify-send`), one per unresolved incident, never for offline. Everything is logged to `state/scheduler.log`.

Incidents also land in `state/alert-pending.json` (kind, message, since, emailed). Incident kinds: three consecutive non-network failures, disk full, unresolved conflict, and **stale**: the Mac answers on port 22 but neither conversations nor memories have succeeded for 2 hours. Two healthy checks delete the file (silent recovery). A Claude desktop scheduled task, **Sync alert email** (`~/.claude/scheduled-tasks/agent-sync-alert-email`, every 2 h while the Claude app is open on Linux), reads that file and emails the `alert_email` in `config.json` once per incident through the Gmail connector, then marks it `emailed`. No email means either healthy, offline, or the Claude app was closed.

```sh
systemctl --user list-timers agent-sync.timer     # next/last run
systemctl --user start agent-sync.service         # run now
journalctl --user -u agent-sync.service -n 50     # output
tail -n 20 /home/<linux-user>/Documents/Codex/agent-sync/state/scheduler.log
```

## Claude desktop app sidebar records (`app_sessions_sync.py`)

The desktop app lists sessions from its own store (`~/.config/Claude/claude-code-sessions/<account>/<org>/local_*.json` on Linux, `~/Library/Application Support/Claude/claude-code-sessions/...` on the Mac), not from `~/.claude/projects`, so synced transcripts stay invisible until a record exists too. This script ships records both ways for sessions whose working folder exists on the other machine after the prefix swap (`/Users/<mac-user>` <-> `/home/<linux-user>`, `~/Documents/GitHub/<repo>` <-> `~/GitHub/<repo>` matched case-insensitively, top-level home folders such as Downloads), rewrites `cwd`, drops stale error fields, and moves the already-synced transcript under the matching project folder so the app can open and resume it. Never deletes; overwrites a record only when the source has newer activity; backups under `state/app-sessions-backups/<run>/`. Skipped and reported: scratch-workspace sessions, folders missing on the other side, sessions whose transcript no longer exists anywhere, and pre-setup Linux sessions. The app reads its store at startup, so new entries appear after a restart. Enabled on 2026-09-19 (`state/app-sessions.enabled`; delete it to stop); 56 Mac sessions were imported in the first run. `python3 app_sessions_sync.py` previews.

## Claude Code memory sync (`memory_sync.py`)

Two-way sync of `~/.claude/projects/<project>/memory/**` between the machines. Projects are paired by normalized repo name across the two GitHub roots (`/home/<linux-user>/GitHub` and `/Users/<mac-user>/Documents/GitHub`), plus the two home-folder projects and top-level home folders present on both machines (e.g. Downloads). Newest file wins; files changed in the last 60 s are skipped; same-second edits on both sides are left alone and reported as conflicts. `MEMORY.md` indexes are merged (union of lines, keyed by link target) rather than overwritten. Nothing is ever deleted; every overwritten file is backed up to `state/memory-backups/<run>/` on the machine where it was replaced. Projects with no counterpart on the other machine are listed as `unmapped` in `state/memory-latest.json`. The global `~/.claude/CLAUDE.md` files are machine-specific and are not synced. Preview with `python3 memory_sync.py`; the script copies itself to the Mac on each run.

## Verified initial result

- 824 Codex history files copied Mac to Linux; all 824 verified readable through Codex's desktop app server. 757 were listed in its general catalog; the remainder were verified using direct history reads.
- 78 main Claude conversations plus associated files copied Mac to Linux (890 files total).
- One new main Claude conversation plus two associated files copied Linux to Mac.
- Five Mac Codex files and four Linux Claude files were deferred during the initial transfer because they were active/recently changing. Subsequent checks retry deferred files and newly completed conversations.
- Checksums were independently verified after installation on each destination.
- Eight regression checks cover the historical exclusion, attachment ownership, identity matching, invalid paths, active Codex writers, divergent histories, and changes during transfer.

## Implementation and recovery

agent-sync 0.7.1 is installed on Linux. Its unrestricted sync command does not implement this cutoff, so the scheduled transfer uses a custom filtered helper with SSH/rsync. Do not replace it with unrestricted agent-sync sync: that would upload old Linux history.

Linux setup directory: `/home/<linux-user>/Documents/Codex/agent-sync`.
Mac endpoint directory: `/Users/<mac-user>/Documents/Codex/agent-sync`.
Mac connection: the Mac user and address from `config.json` through Tailscale. Its SSH key was verified against the same trusted Mac key used over Ethernet. No direct Ethernet cable is required.

The frozen baseline JSON files define which Linux conversations must stay off the Mac. Do not delete or recreate those baselines. Staged source snapshots, transfer reports, and backups of replaced destination files are retained in the setup directories' `state` subdirectories. Original source histories remain in place.

Read-only preview on Linux:

```sh
python3 /home/<linux-user>/Documents/Codex/agent-sync/sync_sessions.py
```

Apply eligible changes:

```sh
python3 /home/<linux-user>/Documents/Codex/agent-sync/sync_sessions.py --apply
```

The helper defers active files and refuses divergent overwrites; it does not stop apps or resolve conflicts by choosing an arbitrary winner.

## Failure alerts

The five-minute automation runs `check_sync_health.py`, which invokes the original safe sync. It saves health status to `state/sync-health.json`.

- Three consecutive non-network failures trigger a desktop alert through `scheduled_sync.py`. Network outages and sleeping/offline devices are recorded as waiting for connection, with no alert regardless of duration.
- A disk-full error or unresolved history conflict triggers an immediate alert.
- Only one problem alert is emitted per unresolved incident; there are no daily reminders.
- Two consecutive successful checks silently clear an incident; recovery notices are disabled. A brief success followed by another failure does not generate repeated alerts.
- Open conversations and overlapping checks are normal deferrals.
- If the checker itself fails, the automation reports a new monitoring failure and suppresses repeated notices for the same unresolved failure.

Current desktop alerts use `scheduled_sync.py` and `notify-send`; they require the Linux scheduler to run. The separate Claude email task described above consumes `state/alert-pending.json` when that app is available. Phone push delivery has not been verified. Endpoint cleanup removes completed/stale staging and retains replacement backups for 14 days by default; storage reservations are not implemented.
