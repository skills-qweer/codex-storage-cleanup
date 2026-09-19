# Handoff

Read `AGENTS.md` and `CURRENT_TASK.md` before acting. `CURRENT_TASK.md` is the only active plan; if it says `NONE / IDLE`, do not infer new work from old conversation or Git history.

Repository truth:

- Canonical checkout and installed skill target: `E:\skills\codex-storage-cleanup`.
- Installed path `D:\CodexHome\skills\codex-storage-cleanup` is a junction to that checkout.
- Publish target is `origin/main`; preserve unrelated work and verify a clean tree before and after publishing.
- Live cleanup target is `D:\CodexHome`; main conversations, archived conversations, and user assets are never completed-subagent candidates.

Execution rules:

- Discover the live desktop app-server through its signed OpenAI parent and use that actual signed executable. Do not restore WindowsApps/AppData layout restrictions or a required plugin mirror.
- Native compatibility checks required protocol request shapes and database fields. Additive upgrades and calendar dates do not block it; only known pre-fix releases below `0.145.0` are version-excluded. Legacy `0.142.2` support and profile expiry are incident recovery only.
- Use compact fresh `wait_threads(timeoutMs:0)` evidence for status; do not hydrate full conversation history.
- Root-local change or writer contention means skip that root and continue. Partial deletion, database failure, global status failure, runtime mismatch, or unknown RPC error stops the batch.
- Keep protected IDs as a monotonic union throughout a batch.
- Every round must complete or directly advance an acceptance criterion. After two no-progress rounds, remove detail and take the smallest action that closes a criterion.

Current state: native compatibility tolerates packaging/version/additive-schema changes; its regression baseline is 85 passing tests. The latest real cleanup on 2026-09-19 used backend `0.155.0-alpha.9.2`, deleted 7 completed roots / records, skipped 24, failed 0, and reclaimed 51,963,643 bytes. All 47 protected records remained readable; deleted rows/files/spawn edges were absent and four database quick checks passed. Evidence and backups: `C:\Users\13662\Documents\Codex\2026-08-01\codex-storage-cleanup\runs\manual-20260919T035958Z`. Automation remains off. Native evidence is schema v3; start a fresh inventory instead of resuming old v2 evidence. `CURRENT_TASK.md` is `NONE / IDLE`; wait for a new user request.
