#!/usr/bin/env python3
"""Audit or compact selected Codex SQLite databases with offline safety checks."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ALLOWED_DATABASES = {
    "logs_2.sqlite",
    "state_5.sqlite",
    "goals_1.sqlite",
    "memories_1.sqlite",
}
CONFIRM_TOKEN = "MAINTAIN_CODEX_SQLITE"


def active_codex_processes() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    completed = subprocess.run(
        ["tasklist.exe", "/fo", "csv", "/nh"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    rows: list[dict[str, str]] = []
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) < 2:
            continue
        name = row[0]
        if name.casefold() in {"chatgpt.exe", "codex.exe"}:
            rows.append({"name": name, "pid": row[1]})
    return rows


def readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)


def database_stats(path: Path) -> dict[str, object]:
    with readonly_connection(path) as connection:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    return {
        "path": str(path),
        "database_bytes": path.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "internal_free_bytes": page_size * freelist_count,
        "estimated_used_bytes": page_size * (page_count - freelist_count),
        "quick_check": quick_check,
    }


def is_below(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(parent)]).casefold() == str(parent).casefold()
    except ValueError:
        return False


def compact_database(database: Path, backup_dir: Path) -> tuple[Path, object]:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{database.name}.{timestamp}.vacuum-backup.sqlite"
    if backup_path.exists():
        raise RuntimeError(f"Backup path already exists: {backup_path}")

    connection = sqlite3.connect(str(database), timeout=60, isolation_level=None)
    try:
        quick_before = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_before != "ok":
            raise RuntimeError(f"Live database quick_check failed before maintenance: {quick_before}")

        quoted_backup = str(backup_path).replace("'", "''")
        connection.execute(f"VACUUM INTO '{quoted_backup}'")
        with readonly_connection(backup_path) as backup_connection:
            backup_check = str(backup_connection.execute("PRAGMA quick_check").fetchone()[0])
        if backup_check != "ok":
            raise RuntimeError(f"Backup quick_check failed: {backup_check}")

        checkpoint_before = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.execute("VACUUM")
        checkpoint_after = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        quick_after = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_after != "ok":
            raise RuntimeError(f"Live database quick_check failed after maintenance: {quick_after}")
    finally:
        connection.close()

    return backup_path, {
        "backup_quick_check": backup_check,
        "checkpoint_before": checkpoint_before,
        "checkpoint_after": checkpoint_after,
        "live_quick_check_after": quick_after,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=r"D:\CodexHome")
    parser.add_argument("--database", default="logs_2.sqlite", choices=sorted(ALLOWED_DATABASES))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--backup-dir")
    parser.add_argument("--confirm-token", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home).resolve()
    database = (codex_home / args.database).resolve()
    if not database.exists() or not database.is_file():
        raise FileNotFoundError(database)
    if database.parent != codex_home:
        raise RuntimeError("Database escaped CodexHome")

    processes = active_codex_processes()
    before = database_stats(database)
    output: dict[str, object] = {
        "schema_version": 1,
        "mode": "execute" if args.execute else "audit",
        "active_codex_processes": processes,
        "can_execute": not processes,
        "before": before,
        "backup_path": None,
        "maintenance": None,
        "after": None,
    }

    if not args.execute:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    if args.confirm_token != CONFIRM_TOKEN:
        raise RuntimeError(f"Execution requires --confirm-token {CONFIRM_TOKEN}")
    if processes:
        raise RuntimeError("Codex/ChatGPT processes are still running; offline maintenance refused")
    if not args.backup_dir:
        raise RuntimeError("Execution requires --backup-dir outside CodexHome")

    backup_dir = Path(args.backup_dir).resolve()
    if is_below(backup_dir, codex_home):
        raise RuntimeError("Backup directory must be outside CodexHome")
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_path, maintenance = compact_database(database, backup_dir)
    output["backup_path"] = str(backup_path)
    output["maintenance"] = maintenance
    output["after"] = database_stats(database)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=list))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # concise machine-readable failure
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
