---
name: codex-storage-cleanup
description: Audit and safely reclaim disk space under CodexHome on Windows, including stale tool backups and installers, plugin staging/cache, sandbox logs, SQLite free pages, and completed subagent records. Use when the user asks why CodexHome is large, requests Codex storage cleanup or ended-subagent cleanup, or says “检查占用空间”, “清理 Codex 缓存”, “删除已结束子代理”, or similar. Never automatically delete main or archived conversations or user artifacts.
---

# Codex Storage Cleanup

Audit first, classify every byte, and keep destructive work narrow and verifiable. Treat conversation records and user artifacts as data, not cache.

## Safety contract

- Default to read-only audit. Show exact targets and bytes before deletion.
- Never automatically delete `sessions`, `archived_sessions`, main threads, generated images, visualizations, attachments, memories, credentials, configuration, installed skills, plugins, packages, or runtime binaries.
- Treat subagent cleanup as a separate explicit operation. Protect every active main thread and active subagent; skip ambiguous status.
- Resolve every destructive target to an absolute path below the requested CodexHome. Refuse reparse points and paths that escape the expected parent.
- Do not run cache cleanup or SQLite maintenance while `ChatGPT.exe` or `codex.exe` is running. A skill invocation inside Codex can audit and prepare the command, but offline execution must happen after the user exits Codex and any VS Code Codex extension hosts.
- Before subagent or database mutation, create a manifest and a recoverable backup outside CodexHome. Use a small canary first.
- Preserve unrelated user changes and never create or alter a project repository unless the user placed it in scope.

## 1. Audit storage

Run the bundled read-only scanner:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\audit_storage.ps1 -CodexHome 'D:\CodexHome'
```

Report:

- total logical bytes and junctions skipped;
- top-level directory sizes and largest files;
- main and archived conversation totals;
- known safe-cleanup candidates;
- active Codex/ChatGPT process count;
- user-artifact totals.

Resolve current thread status with the Codex app thread tools when available. Folder age alone never proves that a conversation is disposable.

## 2. Clean redundant non-conversation data

Always run a dry plan first:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OnlineSafe -CodexHome 'D:\CodexHome'
```

`OnlineSafe` only proposes:

- a `LibreOffice-*-broken-backup-*` directory when a separate working installation exists and passes `soffice --version`;
- matching LibreOffice installer downloads when that installed version exists.

After the user confirms the exact plan, execute with the required token:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OnlineSafe -CodexHome 'D:\CodexHome' -Execute -ConfirmToken CLEAN_CODEX_STORAGE
```

For plugin staging, rebuildable cache, and old sandbox logs, use `OfflineSafe`. The script must refuse execution if any Codex/ChatGPT process remains:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OfflineSafe -CodexHome 'D:\CodexHome'
```

After the user reviews the plan, provide this external command for execution after Codex is fully closed:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OfflineSafe -CodexHome 'D:\CodexHome' -Execute -ConfirmToken CLEAN_CODEX_STORAGE
```

Never delete the whole `.tmp`, `plugins`, `packages`, `.sandbox-bin`, `skills`, or `tools` directory.

## 3. Reclaim SQLite free pages

Audit the log database without mutation:

```powershell
python scripts\maintain_sqlite.py --codex-home 'D:\CodexHome' --database logs_2.sqlite
```

Only after all Codex processes are closed, run maintenance with a backup directory outside CodexHome:

```powershell
python scripts\maintain_sqlite.py --codex-home 'D:\CodexHome' --database logs_2.sqlite --execute --backup-dir 'C:\Codex-maintenance-backups' --confirm-token MAINTAIN_CODEX_SQLITE
```

The script creates a compact `VACUUM INTO` backup, validates it, checkpoints/truncates WAL, vacuums the live database, and runs `quick_check`. Keep the backup until the next successful Codex launch; remove it only after separate user confirmation.

Never delete a SQLite main, WAL, or SHM file individually while Codex is running.

## 4. Audit and clean completed subagents

Generate a read-only inventory. Pass every known active main-thread and subagent ID as `--protect`:

```powershell
python scripts\subagent_inventory.py --codex-home 'D:\CodexHome' --protect THREAD_ID --output 'subagent-manifest.json'
```

The inventory uses only strong evidence: spawn edges, explicit agent metadata, or subagent markers near rollout metadata. Ambiguous records are excluded.

Before deletion:

1. Query current app threads and collaboration agents again.
2. Confirm protected IDs, candidate count, root-subtree count, and exact bytes with the user.
3. Back up `state_5.sqlite*` and each canary rollout outside CodexHome.
4. Check `codex --version` and use the supported `codex delete --force <root-thread-id>` path. Delete candidate roots, not every descendant separately.
5. Run one smallest canary. If deletion reports `agent_jobs`, missing tables, a schema mismatch, a locked file, or partial deletion, stop. Do not hand-delete JSONL or patch the live schema as an automatic fallback.
6. Re-snapshot live agents before the bulk pass. Skip a subtree if it contains a protected ID, a new descendant, or a rollout modified after the manifest.

After deletion, verify all of the following:

- protected main threads still open and remain readable;
- candidate thread rows, spawn edges, and rollout files are absent;
- `PRAGMA quick_check` is `ok` for state, goals, memories, and logs databases;
- skipped and failed counts are reported separately;
- before/after physical size is reported.

Main conversations and archived conversations are outside this module even when idle. Delete them only through a separate, explicitly authorized task-ID selection.

## 5. Report the outcome

Lead with bytes actually reclaimed. State what was deleted, what was preserved, whether deletion is recoverable, what still requires offline work, and the new CodexHome size. Keep manifests and result logs in the current task’s user-facing output directory, not inside CodexHome.
