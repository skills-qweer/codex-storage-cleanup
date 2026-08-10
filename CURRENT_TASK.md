# Current task

Status: ACTIVE

Objective: Make completed-subagent cleanup finish useful work without weakening protections for main conversations or database integrity, publish the change to `main`, sync the installed local skill, and run one real cleanup.

Acceptance criteria:

- Add `scripts/cleanup_completed_subagents.py` with inventory/plan preparation, external online backups, native canary, per-root skip-and-continue, resumable result logging, final verification, and concise progress.
- Treat `active writer`, an existing writer lock, live status change, rollout path/size/mtime change, or protection intersection as a root-local skip.
- Stop the batch only for partial deletion, database failure, incomplete global status, runtime mismatch, or unknown RPC failure.
- Run full preflight only at batch start and immediately before canary; use a lightweight PID, condition key, schema/migration, runtime-file, and monotonic-protection fingerprint for later roots.
- Require fresh `wait_threads(timeoutMs:0)` status evidence and never load full histories for ordinary status checks.
- Keep the protection set as a monotonic union; active-to-idle transitions do not stop the batch.
- Move legacy `0.142.2` recovery detail out of the daily native workflow.
- Add focused tests for local skips, continuation, global stops, and verification; pass all existing tests and skill validation.
- Commit and push the finished change directly to `origin/main`, verify the installed junction sees it, then execute one real cleanup and report deleted/skipped/failed roots and bytes.

Progress:

- Confirmed the previous batch stopped after one successful canary because one unchanged root returned the known `active writer` refusal.
- Confirmed the repository is clean on `main` and matches `origin/main` at `15a0725` before edits.
- Added the file-backed anti-drift workflow; implementation is next.
- Added the formal native cleanup runner with external backups, compact status evidence, root-local skip isolation, resumable logs, and final verification.
- Replaced the daily skill/README workflow so full preflight is limited to batch start and canary; legacy recovery is now conditional reference material.
- Added focused tests for writer locks, active-writer refusal, mtime drift, continuation, monotonic protection, unknown RPC, global status loss, and partial deletion.
- Passed all 60 unit tests, Python compilation, skill validation, and `git diff --check`.

Next action: publish the validated change to `origin/main`, verify the installed junction, and execute one real cleanup.
