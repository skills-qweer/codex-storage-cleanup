---
name: codex-storage-cleanup
description: Audit and safely reclaim disk space under CodexHome on Windows, including stale tool backups and installers, plugin staging/cache, sandbox logs, SQLite free pages, and completed subagent records. Use when the user asks why CodexHome is large, requests Codex storage cleanup or ended-subagent cleanup, or says “检查占用空间”, “清理 Codex 缓存”, “删除已结束子代理”, or similar. Never automatically delete main or archived conversations or user artifacts.
---

# Codex Storage Cleanup

Audit first. Treat conversations and user artifacts as data, not cache. Keep normal cleanup direct; isolate a changed or busy subagent root instead of escalating it into a batch-wide failure.

## Safety contract

- Default to read-only audit. Destructive work requires explicit user authorization, but a standing authorization may be used without asking again.
- Never automatically delete main threads, archived threads, generated images, visualizations, attachments, memories, credentials, configuration, installed skills, plugins, packages, or runtimes.
- Resolve every target below the requested CodexHome and refuse reparse points or path escapes.
- Protect active, interrupted, unknown, recent, or potentially reusable tasks. A completed descendant of an active main task is still eligible when its own whole subtree is independently completed and unprotected.
- Back up `state_5.sqlite`, `goals_1.sqlite`, and `memories_1.sqlite` with SQLite online backup outside CodexHome before subagent deletion. Keep the compact manifest and results outside CodexHome.
- Use only the native executable selected by a fresh matched-runtime preflight and prove one real canary before treating later successful deletions as normal batch work.
- Preserve unrelated user changes.

## 1. Audit storage

Run the bundled read-only scanner:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\audit_storage.ps1 -CodexHome 'D:\CodexHome'
```

Report total bytes, top-level sizes, largest files, main/archived conversation totals, safe-cleanup candidates, active processes, and user-artifact totals. Folder age alone never proves that a conversation is disposable.

## 2. Clean redundant non-conversation data

Plan online-safe targets first:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OnlineSafe -CodexHome 'D:\CodexHome'
```

After authorization, execute only the printed plan:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OnlineSafe -CodexHome 'D:\CodexHome' -Execute -ConfirmToken CLEAN_CODEX_STORAGE
```

Use `OfflineSafe` only after Codex, ChatGPT, VS Code Codex hosts, and `codex.exe` are closed:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OfflineSafe -CodexHome 'D:\CodexHome'
```

Never delete the whole `.tmp`, `plugins`, `packages`, `.sandbox-bin`, `skills`, or `tools` directory.

## 3. Reclaim SQLite free pages

Audit without mutation:

```powershell
python scripts\maintain_sqlite.py --codex-home 'D:\CodexHome' --database logs_2.sqlite
```

Run maintenance only while Codex is closed and only with an external backup directory:

```powershell
python scripts\maintain_sqlite.py --codex-home 'D:\CodexHome' --database logs_2.sqlite --execute --backup-dir 'C:\Codex-maintenance-backups' --confirm-token MAINTAIN_CODEX_SQLITE
```

Never delete a SQLite main, WAL, or SHM file individually.

## 4. Clean completed subagents

Use `scripts/cleanup_completed_subagents.py` for the normal native path. Do not reconstruct an ad-hoc deletion loop.

### Inventory and compact live status

First query the global app task list and collaboration agents. The sources must be complete. Build a monotonic protected set: add every active, interrupted, unknown, recent, or potentially reusable main/subagent ID, and never remove an ID during the batch merely because it changes from active to idle.

Run inventory into a new directory outside CodexHome. This command performs the batch-start matched-runtime preflight before scanning rollouts:

```powershell
python scripts\cleanup_completed_subagents.py inventory `
  --codex-home 'D:\CodexHome' `
  --run-dir 'C:\Codex-cleanup\run-YYYYMMDD-HHMMSS' `
  --protect THREAD_ID
```

Read `status-targets.json`. Query those task IDs with `wait_threads` in groups of at most eight and `timeoutMs: 0`. Do not use `read_thread` for ordinary status checks; it hydrates unnecessary history. Retry an individual snapshot only when needed. An individual unavailable or changed task becomes an unknown root-local status and is skipped; an unavailable global list/host/source stops the batch.

Write one fresh external `status-evidence.json`:

```json
{
  "schema_version": 1,
  "captured_at": "2026-08-11T12:00:00+08:00",
  "global_complete": true,
  "unavailable_sources": [],
  "unavailable_hosts": [],
  "protected_ids": ["ACTIVE_OR_UNCERTAIN_ID"],
  "threads": {
    "SUBAGENT_ID": {
      "status": "completed",
      "latest_turn_status": "completed",
      "host_id": "local"
    }
  }
}
```

Only `completed`/`complete` is deletion evidence. `idle` describes the task container and is not a substitute for the latest turn result. Strong subagent evidence, a complete finished subtree, non-archived state, the minimum idle interval, and no protected member are all required.

### Back up, canary, continue, and verify

With explicit or standing deletion authorization, run:

```powershell
python scripts\cleanup_completed_subagents.py run `
  --codex-home 'D:\CodexHome' `
  --run-dir 'C:\Codex-cleanup\run-YYYYMMDD-HHMMSS' `
  --status-evidence 'C:\Codex-cleanup\run-YYYYMMDD-HHMMSS\status-evidence.json' `
  --protect THREAD_ID
```

The runner:

1. Reuses a still-fresh batch-start preflight or refreshes it when stale.
2. Snapshots exact subtree rows, edge status, rollout path, bytes, and mtime.
3. Creates and verifies external SQLite online backups.
4. Runs one full preflight immediately before the first native canary and requires the same `condition_key`.
5. Keeps one validated local app-server for the batch. Before each root it checks only the saved condition key, desktop PID, runtime file size/mtime, SQLite schema and migration fingerprint, monotonic protection hash, live subtree, row state, and rollout snapshot.
6. Writes a result after every root, prints one compact progress record per ten handled roots, and verifies every successful deletion immediately.
7. Verifies deleted rows, spawn edges, rollout files, protected records, and `quick_check` for state/goals/memories/logs at the end.

If execution is interrupted without an integrity stop, rerun the same command with `--resume`. Already recorded roots are not repeated. A changed runtime condition or smaller protection set is not accepted for resume.

### Failure isolation

| Condition | Action |
| --- | --- |
| Existing writer lock on any member | Skip that root; continue independent roots |
| Exact app-server `-32600` “already has an active writer”, with all rows/files still present | Skip that root; continue |
| Latest status, subtree, protected intersection, row state, rollout path/size/mtime changed | Skip that root; continue |
| Root already fully absent without a prior result | Record a skip; do not count its bytes as reclaimed by this run |
| Partial deletion: only some expected rows/edges/files remain | Stop the whole batch and preserve evidence |
| SQLite error or failed `quick_check` | Stop the whole batch |
| Global task/collaboration source is incomplete or unavailable | Stop the whole batch |
| Desktop PID, condition key, matched runtime, schema, or migration fingerprint changes | Stop the whole batch |
| Timeout or RPC error other than the exact unchanged active-writer refusal | Stop the whole batch |

Do not turn a root-local refusal into a batch stop. Do not turn an unknown/global integrity condition into a skip.

The normal path never installs compatibility objects. Legacy `0.142.2` recovery is only for an already-existing, exact reviewed partial-deletion incident; read [references/subagent-delete-compatibility.md](references/subagent-delete-compatibility.md) and the referenced machine profiles only when such an incident exists. Never use legacy recovery to start a new canary.

Main and archived conversations remain outside this module even when idle. Deleting them requires a separate, explicitly selected task-ID operation.

## 5. Compatibility freshness and updates

Run the read-only update check when a preflight reports an unsupported or stale runtime:

```powershell
python scripts\refresh_skill.py check
```

Do not update the skill or Codex CLI as a side effect of ordinary cleanup. Apply only an explicitly authorized, validated fast-forward from the trusted repository. Keep legacy install/remove evidence, tokens, journals, and the 24-hour recovery rules in [references/subagent-delete-compatibility.md](references/subagent-delete-compatibility.md), outside the daily workflow.

## 6. Report the outcome

Lead with bytes actually reclaimed. Report deleted root/thread counts, skipped roots grouped by reason, failed roots, new CodexHome size, database verification, and the external run directory. State that deleted rollout history is irreversible unless the retained database backups are sufficient for the user's recovery needs. Do not delete backups, manifests, or audit logs without a separate request.
