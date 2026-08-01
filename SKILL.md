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
- Before database maintenance, create a recoverable database backup outside CodexHome. Before subagent cleanup, write a manifest and temporary online backups of `state_5.sqlite`, `goals_1.sqlite`, and `memories_1.sqlite` outside CodexHome; do not duplicate every rollout by default. Use one candidate root as a canary.
- Never infer delete compatibility from `codex --version` alone. Require the CLI version, migration maximum and checksums, SQLite object fingerprints, exact canary result, and fresh backup evidence to match a reviewed profile.
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
3. Use the SQLite online-backup API to snapshot `state_5.sqlite`, `goals_1.sqlite`, and `memories_1.sqlite` into a temporary directory outside CodexHome. Keep the manifest and summary separately. Do not copy all candidate rollout files unless the user explicitly requests a full rollback archive; normal bulk deletion is irreversible.
4. Run the bundled read-only compatibility diagnosis before the canary. A known profile still requires a real canary; an unknown or expired profile stops deletion.
5. Check `codex --version` and run one candidate root through `codex delete --force <root-thread-id>`. Delete a root once, not every descendant separately.
6. If the canary fails, save the raw CLI/app-server error and the exact partial-deletion state. Re-run diagnosis with that evidence and a newly generated external backup summary. Only `known_workaround_eligible` may proceed to a separately authorized temporary install.
7. For a large batch, use the local `codex app-server --stdio` `thread/delete` method one candidate root at a time. Before every root, compare the live subtree with the manifest, reject protected or unexpected descendants, and skip rollout files modified after the snapshot. Stop on a timeout, lock, RPC failure, or partial deletion.
8. Re-snapshot live app threads and collaboration agents before each batch, including descendants under an active main thread. A completed descendant may be deleted only when its live task status is finished and its subtree contains no protected ID.

Run the read-only diagnosis from the skill root:

```powershell
python scripts\subagent_delete_compat.py diagnose --codex-home 'D:\CodexHome'
```

For a failed canary, pass the unedited incident evidence and fresh backup summary:

```powershell
python scripts\subagent_delete_compat.py diagnose `
  --codex-home 'D:\CodexHome' `
  --failure-evidence 'C:\Codex-cleanup\canary-failure.json' `
  --backup-summary 'C:\Codex-cleanup\db-backup-summary.json'
```

Interpret decisions exactly:

- `canary_required`: run one official canary; never install the workaround pre-emptively.
- `known_workaround_eligible`: the only state in which an explicitly authorized compatibility install may be planned.
- `compat_installed`: do not reinstall; continue only the original incident and retain the matching install result for removal.
- `stale_profile_update_required` or `unsupported_update_required`: stop deletion and use the update lifecycle below.
- `unsafe_stop`: preserve evidence and backups; do not retry, repair, or generalize the old fix.

The currently reviewed workaround covers only CLI `0.142.2` plus successful migration 42 and the exact `no such table: agent_jobs` canary fingerprint. Read [references/subagent-delete-compatibility.md](references/subagent-delete-compatibility.md) and the machine profile before using it. Any different version, migration checksum, missing table, partial object set, non-empty compatibility table, lock, I/O error, failed backup, failed `quick_check`, timeout, protected-ID drift, or second canary failure must stop.

`install` and `remove` default to dry plans. Execution requires their printed confirmation tokens, audit output outside CodexHome, and separate user authorization. Live writes accept only the bundled profile from a clean reviewed repository where `main == origin/main`, plus the non-reparse `CodexHome\state_5.sqlite`. Install writes a `prepared` journal before its transaction, rechecks the live canary row, exact spawn-edge set, and rollout state, creates only the four reviewed empty objects, writes a `commit-pending` journal containing the resulting SQLite `schema_version` and object rootpages, then commits. It never edits `_sqlx_migrations` or calls a delete API. Failure evidence is valid for at most 24 hours. Automatic removal is limited to 24 hours and requires the matching journal, current CLI, run nonce, database identity, unchanged profile/evidence/backup hashes, unchanged `schema_version` and rootpages, exact object hashes, and empty tables. Do not hand-write or copy DDL from another database.

After deletion, verify all of the following:

- protected main threads still open and remain readable;
- candidate thread rows, spawn edges, and rollout files are absent;
- `PRAGMA quick_check` is `ok` for state, goals, memories, and logs databases;
- skipped and failed counts are reported separately;
- before/after physical size is reported.

After every check passes, remove any temporary compatibility objects and run `quick_check` again. If the user wants net disk reclamation, delete the temporary database backups and helper files after reporting that the deleted subagent history is then no longer recoverable. Keep the compact manifest and result logs in the task output directory.

Main conversations and archived conversations are outside this module even when idle. Delete them only through a separate, explicitly authorized task-ID selection.

## 5. Compatibility freshness and skill updates

Treat a profile as stale after its `review_after` date, when the CLI/schema combination is absent, or when the observed error/protocol differs. A newer Codex version is not assumed fixed: first update the compatibility knowledge, then run a new read-only diagnosis and one new canary. Never continue a partially deleted incident merely because source code was refreshed.

Check the trusted repository without changing it:

```powershell
python scripts\refresh_skill.py check
```

If the check reports `trusted_fast_forward_available` and the user explicitly authorized updating the skill, apply only the validated fast-forward:

```powershell
python scripts\refresh_skill.py apply `
  --confirm-no-active-cleanup `
  --execute `
  --confirm-token UPDATE_CODEX_STORAGE_SKILL
```

When an update check is caused by a failed cleanup, pass `--incident-evidence`. The updater must refuse if it records partial deletion, bulk progress, an installed workaround, or an unknown evidence schema. An update unrelated to cleanup requires `--confirm-no-active-cleanup`. For `stale_profile_update_required` or `unsupported_update_required`, save diagnosis output and pass it as `--compat-diagnosis`; no newer upstream revision then becomes `draft_pr_required` instead of `up_to_date`.

The updater also refuses a dirty tree, non-`main` branch, unexpected origin, diverged history, disabled TLS verification, network/authentication failure, or failed validation. It statically checks fetched front matter, JSON, Python compilation, PowerShell syntax, and test presence without executing newly fetched test code with the user's files, network, or Git credentials. It records the exact fetched commit, validates that commit in the temporary worktree, then rechecks HEAD, worktree, origin, the local `origin/main` value, remote SHA, and incident evidence immediately before merging that exact SHA with `--ff-only`. It never stashes, resets, rebases, force-pushes, changes remotes, updates the Codex CLI, opens Codex databases, invokes deletion, pushes a branch, or merges a remote PR. Automatic fast-forward trusts the allow-listed `origin/main` maintainers and review process; static checks are not a proof that remote source is harmless.

If trusted `origin/main` has no reviewed support for the new combination, stop live deletion. With explicit repository-update authorization, collect sanitized primary evidence, update the profile/script/docs and fixtures on a feature branch, run all tests, push only that branch, and create a Draft PR. Never commit secrets, write directly to `main`, auto-merge, or invent schema from an unknown database. Re-run diagnosis after a reviewed update; do not automatically retry the failed canary.

## 6. Report the outcome

Lead with bytes actually reclaimed. State what was deleted, what was preserved, whether deletion is recoverable, what still requires offline work, and the new CodexHome size. Keep manifests and result logs in the current task’s user-facing output directory, not inside CodexHome.
