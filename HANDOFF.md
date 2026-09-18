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

Current state: native compatibility now tolerates packaging/version/additive-schema changes without per-release patches. All 85 tests and skill validation pass. Desktop backend `0.155.0-alpha.9.2` passed preflight and isolated initialize/thread-list validation on 2026-09-19; no real records were deleted and no automation was restarted. Read-only evidence: `C:\Users\13662\Documents\Codex\2026-08-01\codex-storage-cleanup\runs\compatibility-20260919-074041\native-preflight.json`. Native evidence is schema v3; start a fresh inventory instead of resuming old v2 evidence. A real cleanup still needs current activity evidence, backups, and canary. `CURRENT_TASK.md` is `NONE / IDLE`; wait for a new user request.
