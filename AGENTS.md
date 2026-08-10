# Project workflow

This repository uses one file-backed work package so long conversations cannot silently change the task.

Before editing, testing, committing, publishing, or running live cleanup:

1. Read `CURRENT_TASK.md` and `HANDOFF.md`.
2. Confirm the next action advances a listed acceptance criterion.
3. Compare the current scope with the previous progress entry. If two consecutive rounds add detail without completing a criterion, narrow the approach and finish the smallest useful change.
4. Do not recover work from chat history, Git history, or stale notes when `CURRENT_TASK.md` says `NONE / IDLE`.

Keep exactly one active plan in `CURRENT_TASK.md`. Replace it when the user changes direction. Delete abandoned, superseded, speculative, or stale plans instead of archiving or mentioning them in project records.

Append only verified completed outcomes to `COMPLETED_WORK.md`; never archive unfinished or abandoned plans there. Update `HANDOFF.md` with durable rules, current repository truth, and the single next action. Keep all three files concise.

Do not expand a bounded fix into a framework, new policy system, or unrelated refactor. Safety checks must correspond to a demonstrated failure mode and must distinguish root-local skips from batch-global stops.
