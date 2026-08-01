#!/usr/bin/env python3
"""Build a read-only manifest of strongly identified Codex subagent threads."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
AGENT_MARKERS = ("subagent", "sub-agent", "spawn_agent", "spawn-agent", "collaboration")
ENDED_STATUSES = {"completed", "complete", "failed", "cancelled", "canceled", "shutdown", "closed"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=r"D:\CodexHome")
    parser.add_argument("--protect", action="append", default=[])
    parser.add_argument("--output")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--metadata-max-bytes", type=int, default=2 * 1024 * 1024)
    return parser.parse_args()


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def normalize_rollout_path(raw: str) -> Path:
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return Path(raw).resolve()


def is_below(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(parent)]).casefold() == str(parent).casefold()
    except ValueError:
        return False


def marker_in_source(value: Any) -> bool:
    """Inspect only structured source metadata, never instructions or message text."""
    if isinstance(value, str):
        lowered = value.casefold()
        return any(marker in lowered for marker in AGENT_MARKERS)
    if isinstance(value, dict):
        return any(marker_in_source(key) or marker_in_source(child) for key, child in value.items())
    if isinstance(value, list):
        return any(marker_in_source(child) for child in value)
    return False


def find_parent_id_in_source(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered_key = str(key).casefold()
            if lowered_key in {"parent_thread_id", "parent_id", "parent_thread"}:
                candidate = str(child)
                if UUID_RE.match(candidate):
                    return candidate
            nested = find_parent_id_in_source(child)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = find_parent_id_in_source(child)
            if nested:
                return nested
    return None


def rollout_metadata(path: Path, byte_limit: int) -> tuple[bool, str | None]:
    consumed = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(80):
                line = handle.readline()
                if not line:
                    break
                consumed += len(line.encode("utf-8", errors="replace"))
                if consumed > byte_limit:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if str(record.get("type", "")).casefold() != "session_meta":
                    continue
                payload = record.get("payload", record)
                if not isinstance(payload, dict):
                    return False, None
                source = payload.get("source")
                marker = bool(
                    payload.get("agent_nickname")
                    or payload.get("agent_role")
                    or payload.get("agent_path")
                    or marker_in_source(payload.get("thread_source"))
                    or marker_in_source(source)
                )
                parent = payload.get("parent_thread_id")
                if not (isinstance(parent, str) and UUID_RE.match(parent)):
                    parent = find_parent_id_in_source(source)
                return marker, parent if isinstance(parent, str) and UUID_RE.match(parent) else None
    except (OSError, UnicodeError):
        return False, None
    return False, None


def descendants(root: str, children: dict[str, set[str]], allowed: set[str]) -> list[str]:
    found: list[str] = []
    queue: deque[str] = deque([root])
    seen: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in seen or current not in allowed:
            continue
        seen.add(current)
        found.append(current)
        queue.extend(sorted(children.get(current, set())))
    return found


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home).resolve()
    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        raise FileNotFoundError(state_db)

    protected = {item.casefold() for item in args.protect if UUID_RE.match(item)}
    reasons: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, str] = {}
    children: dict[str, set[str]] = defaultdict(set)
    edge_status: dict[str, str] = {}

    with readonly_connection(state_db) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        thread_rows = {str(row["id"]): dict(row) for row in connection.execute("SELECT * FROM threads")}

        if table_exists(connection, "thread_spawn_edges"):
            for edge in connection.execute(
                "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"
            ):
                parent = str(edge["parent_thread_id"])
                child = str(edge["child_thread_id"])
                parents[child] = parent
                children[parent].add(child)
                edge_status[child] = str(edge["status"] or "")
                reasons[child].add("spawn_edge")

    for thread_id, row in thread_rows.items():
        if any(row.get(field) for field in ("agent_nickname", "agent_role", "agent_path")):
            reasons[thread_id].add("explicit_agent_metadata")
        source_values = " ".join(
            str(row.get(field) or "") for field in ("source", "thread_source")
        ).casefold()
        if any(marker in source_values for marker in AGENT_MARKERS):
            reasons[thread_id].add("agent_source")

        raw_path = str(row.get("rollout_path") or "")
        if not raw_path:
            continue
        try:
            rollout = normalize_rollout_path(raw_path)
        except OSError:
            continue
        if not rollout.exists() or not rollout.is_file() or not is_below(rollout, codex_home):
            continue
        marker, metadata_parent = rollout_metadata(rollout, args.metadata_max_bytes)
        if marker:
            reasons[thread_id].add("rollout_session_meta")
        if marker and metadata_parent and thread_id not in parents:
            parents[thread_id] = metadata_parent
            children[metadata_parent].add(thread_id)
            reasons[thread_id].add("rollout_parent_metadata")

    candidate_ids = set(reasons)
    roots = sorted(thread_id for thread_id in candidate_ids if parents.get(thread_id) not in candidate_ids)
    candidate_rows: list[dict[str, object]] = []
    sizes: dict[str, int] = {}

    for thread_id in sorted(candidate_ids):
        row = thread_rows.get(thread_id, {})
        raw_path = str(row.get("rollout_path") or "")
        file_exists = False
        size_bytes = 0
        normalized_path = raw_path
        if raw_path:
            try:
                rollout = normalize_rollout_path(raw_path)
                normalized_path = str(rollout)
                if rollout.exists() and rollout.is_file() and is_below(rollout, codex_home):
                    file_exists = True
                    size_bytes = rollout.stat().st_size
            except OSError:
                pass
        sizes[thread_id] = size_bytes
        status = edge_status.get(thread_id, "")
        candidate_rows.append(
            {
                "id": thread_id,
                "parent_thread_id": parents.get(thread_id),
                "reasons": sorted(reasons[thread_id]),
                "status_from_spawn_edge": status or None,
                "status_looks_ended": status.casefold() in ENDED_STATUSES if status else None,
                "protected": thread_id.casefold() in protected,
                "archived": bool(row.get("archived", 0)),
                "title": str(row.get("title") or ""),
                "agent_nickname": row.get("agent_nickname"),
                "agent_role": row.get("agent_role"),
                "rollout_path": normalized_path,
                "file_exists": file_exists,
                "size_bytes": size_bytes,
            }
        )

    root_rows: list[dict[str, object]] = []
    for root in roots:
        subtree = descendants(root, children, candidate_ids)
        protected_members = sorted(item for item in subtree if item.casefold() in protected)
        non_ended_statuses = {
            item: edge_status[item]
            for item in subtree
            if edge_status.get(item) and edge_status[item].casefold() not in ENDED_STATUSES
        }
        unknown_status_ids = sorted(item for item in subtree if not edge_status.get(item))
        root_rows.append(
            {
                "root_id": root,
                "thread_count": len(subtree),
                "size_bytes": sum(sizes.get(item, 0) for item in subtree),
                "protected_members": protected_members,
                "non_ended_statuses": non_ended_statuses,
                "unknown_status_ids": unknown_status_ids,
                "eligible_from_manifest": not protected_members and not non_ended_statuses and not unknown_status_ids,
                "thread_ids": subtree,
            }
        )

    output: dict[str, object] = {
        "schema_version": 1,
        "codex_home": str(codex_home),
        "state_database": str(state_db),
        "state_quick_check": quick_check,
        "protected_ids": sorted(protected),
        "candidate_count": len(candidate_rows),
        "candidate_root_count": len(root_rows),
        "candidate_bytes": sum(sizes.values()),
        "candidates": candidate_rows,
        "candidate_roots": root_rows,
        "notes": [
            "This is a read-only strong-evidence inventory, not deletion authorization.",
            "Recheck live app and collaboration status immediately before deletion.",
            "Main and archived conversations without subagent evidence are excluded.",
        ],
    }

    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output).resolve()
        if output_path.exists() and not args.overwrite:
            raise FileExistsError(f"Output exists; pass --overwrite to replace it: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
