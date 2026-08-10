# Handoff

Read `AGENTS.md` and `CURRENT_TASK.md` before acting. `CURRENT_TASK.md` is the only active plan; if it says `NONE / IDLE`, do not infer new work from old conversation or Git history.

Repository truth:

- Canonical checkout and installed skill target: `E:\skills\codex-storage-cleanup`.
- Installed path `D:\CodexHome\skills\codex-storage-cleanup` is a junction to that checkout.
- Publish target is `origin/main`; preserve unrelated work and verify a clean tree before and after publishing.
- Live cleanup target is `D:\CodexHome`; main conversations, archived conversations, and user assets are never completed-subagent candidates.

Execution rules:

- Prefer the native matched desktop app-server. Legacy `0.142.2` support is incident recovery only.
- Use compact fresh `wait_threads(timeoutMs:0)` evidence for status; do not hydrate full conversation history.
- Root-local change or writer contention means skip that root and continue. Partial deletion, database failure, global status failure, runtime mismatch, or unknown RPC error stops the batch.
- Keep protected IDs as a monotonic union throughout a batch.
- Every round must complete or directly advance an acceptance criterion. After two no-progress rounds, remove detail and take the smallest action that closes a criterion.

Current state: the completed-subagent cleanup redesign and one live run are finished. `CURRENT_TASK.md` is `NONE / IDLE`; wait for a new user request.
