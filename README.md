# agent-sync

Two-way sync of AI coding-assistant state between a Mac and a Linux workstation:
Codex and Claude Code conversations, Claude Code memory files, and Claude desktop
sidebar records. A systemd user timer runs it every five minutes over SSH/rsync.
It uses no model tokens.

I work across a MacBook and a Linux GPU workstation. Each assistant keeps its
history in machine-local stores, so a conversation started on one machine was
invisible on the other. This closes that gap without ever risking either side's data.

## Design

- **Never destructive.** Nothing is deleted. Every file that gets overwritten is
  backed up first.
- **Prefix-only updates.** An existing conversation on the destination is updated
  only when it is an exact byte-prefix of the incoming version. Diverged or compacted
  histories are left alone and reported as conflicts. The sync never guesses a winner.
- **Defers live files.** Open conversations and files changed in the last two minutes
  wait for the next run.
- **Historical cutoff.** Frozen baseline files list the pre-existing Linux conversations
  that must never upload to the Mac. The cutoff holds even if an old conversation is
  resumed later.
- **Memory sync.** Newest file wins, with a 60 s settle window. Same-second edits on
  both machines are reported as conflicts. `MEMORY.md` indexes are merged by link target
  rather than overwritten.
- **Path mapping.** Desktop sidebar records are rewritten for the other machine (home
  prefix swap; GitHub roots matched case-insensitively), so synced sessions open and
  resume there.
- **Host selection.** Tries Tailscale first, then direct Ethernet. Both host keys are
  pinned.

## Monitoring

`check_sync_health.py` turns run results into deduplicated incidents:

- three consecutive non-network failures
- disk full
- an unresolved conflict
- **stale:** the Mac answers on port 22, but nothing has synced for two hours

Each incident produces exactly one alert, as a desktop notification plus an email.
Being offline is never an alert, and recovery is silent after two healthy checks.

## Layout

| File | Role |
| --- | --- |
| `sync_sessions.py` | Conversation transfer with cutoff, prefix check and deferral |
| `session_endpoint.py` | Conservative transfer endpoint on the Mac side |
| `memory_sync.py` | Claude Code memory sync and `MEMORY.md` merge |
| `app_sessions_sync.py` | Desktop sidebar record sync and path rewriting |
| `codex_regroup.py` | Regroups Codex threads into projects by working folder |
| `check_sync_health.py` | Health check and incident deduplication |
| `scheduled_sync.py` | systemd entry point: health-checked sync, then memory sync |
| `mac_host.py` | Reachable-address selection (Tailscale, then Ethernet) |
| `systemd/` | User service and five-minute timer |

## Setup

```sh
cp config.example.json config.json          # machine-specific values; gitignored
python3 sync_sessions.py                    # read-only preview
python3 sync_sessions.py --apply            # apply eligible changes
systemctl --user enable --now agent-sync.timer
```

The initial migration copied 824 Codex history files and 890 Claude files. Checksums
were verified independently on each destination.

Operational runbook: [docs/OPERATIONS.md](docs/OPERATIONS.md).
