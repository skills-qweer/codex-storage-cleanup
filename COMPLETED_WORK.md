# Completed work

Record only finished, verified outcomes. Do not preserve abandoned or superseded plans here.

- 2026-08-03: Paired native deletion with the running signed desktop backend, added reviewed legacy migration-tail evidence, and merged the compatibility hardening through commit `15a0725`.
- 2026-08-11: Replaced the over-defensive daily subagent workflow with `cleanup_completed_subagents.py`, root-local busy/change isolation, compact status evidence, monotonic protection, resumable results, and concise project handoff files. All 60 tests and skill validation passed; implementation commit `b7ec7be` was pushed to `origin/main` and visible through the installed junction.
- 2026-08-11: Ran the new workflow against `D:\CodexHome`. It deleted 43 roots / 50 completed subagent records and reclaimed 18,444,142,184 bytes; skipped 9 roots (2 interrupted, 3 recent, 4 writer-lock), failed 0, preserved all three protected tasks, passed state/goals/memories/logs `quick_check`, and reduced CodexHome to 12,090,361,762 bytes (11.26 GiB). Evidence remains in `C:\Users\13662\Documents\Codex\2026-08-01\codex-storage-cleanup\runs\manual-improved-20260811-061414`.
