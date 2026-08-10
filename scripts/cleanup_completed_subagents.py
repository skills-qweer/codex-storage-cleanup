#!/usr/bin/env python3
"""Inventory and delete completed Codex subagents with root-local failure isolation.

Live completion evidence comes from the Codex app's compact
``wait_threads(timeoutMs: 0)`` snapshots and is supplied as JSON.  This script
owns the filesystem/SQLite inventory, backups, native canary, batch execution,
resumable result log, and final verification.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DB_NAMES = ("state_5.sqlite", "goals_1.sqlite", "memories_1.sqlite")
CHECK_DB_NAMES = (*DB_NAMES, "logs_2.sqlite")
REPARSE_ATTRIBUTE = 0x400
COMPLETED_STATUSES = {"complete", "completed"}
ACTIVE_WRITER_CODE = -32600
ACTIVE_WRITER_TEXT = "already has an active writer"
DEFAULT_STATUS_MAX_AGE_SECONDS = 15 * 60
DEFAULT_MIN_IDLE_SECONDS = 3 * 60 * 60


class RootSkip(RuntimeError):
    """A root-local condition that must not stop independent roots."""

    def __init__(self, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class BatchStop(RuntimeError):
    """A global or integrity condition that must stop the batch."""

    def __init__(self, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class PartialDeletion(BatchStop):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S-%f")


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BatchStop("invalid_json_document", str(path))
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    return connection


def quick_check(path: Path) -> str:
    try:
        with closing(readonly_connection(path)) as connection:
            return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        raise BatchStop("database_error", {"path": str(path), "error": str(exc)}) from exc


def is_below(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(parent))).casefold() == str(parent).casefold()
    except ValueError:
        return False


def assert_no_reparse(path: Path, stop: Path) -> None:
    current = path
    while True:
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise BatchStop("path_error", {"path": str(current), "error": str(exc)}) from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & REPARSE_ATTRIBUTE:
            raise BatchStop("reparse_path", str(current))
        if current == stop:
            return
        if current.parent == current:
            raise BatchStop("path_escape", str(path))
        current = current.parent


def require_external_run_dir(raw: str, codex_home: Path, *, create: bool) -> Path:
    run_dir = Path(raw).resolve()
    if is_below(run_dir, codex_home):
        raise BatchStop("run_dir_inside_codex_home", str(run_dir))
    if create:
        run_dir.mkdir(parents=True, exist_ok=True)
    if not run_dir.is_dir():
        raise BatchStop("missing_run_dir", str(run_dir))
    metadata = os.lstat(run_dir)
    if run_dir.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE:
        raise BatchStop("reparse_run_dir", str(run_dir))
    return run_dir


def normalize_rollout(raw: str, codex_home: Path, *, must_exist: bool = True) -> Path:
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    try:
        path = Path(raw).resolve(strict=must_exist)
    except OSError as exc:
        raise RootSkip("rollout_missing", {"path": raw, "error": str(exc)}) from exc
    if not is_below(path, codex_home):
        raise BatchStop("rollout_path_escape", str(path))
    assert_no_reparse(path, codex_home)
    return path


def file_snapshot(raw: str, codex_home: Path) -> dict[str, Any]:
    path = normalize_rollout(raw, codex_home)
    metadata = path.stat()
    return {
        "path": str(path),
        "size_bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
    }


def load_state(codex_home: Path) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, str]]:
    try:
        with closing(readonly_connection(codex_home / "state_5.sqlite")) as connection:
            rows = {
                str(row["id"]): dict(row)
                for row in connection.execute("SELECT * FROM threads")
            }
            children: dict[str, set[str]] = defaultdict(set)
            statuses: dict[str, str] = {}
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_spawn_edges'"
            ).fetchone()
            if exists:
                for row in connection.execute(
                    "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"
                ):
                    parent = str(row["parent_thread_id"])
                    child = str(row["child_thread_id"])
                    children[parent].add(child)
                    statuses[child] = str(row["status"] or "")
            return rows, children, statuses
    except sqlite3.Error as exc:
        raise BatchStop("database_error", str(exc)) from exc


def subtree(root_id: str, children: dict[str, set[str]]) -> list[str]:
    pending: deque[str] = deque([root_id])
    found: list[str] = []
    seen: set[str] = set()
    while pending:
        current = pending.popleft()
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        pending.extend(sorted(children.get(current, set())))
    return found


def status_value(item: dict[str, Any]) -> str:
    for key in ("latest_turn_status", "turn_status", "status"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value.casefold()
    return "unknown"


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise BatchStop("invalid_status_evidence", f"missing {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BatchStop("invalid_status_evidence", f"invalid {label}: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_status_evidence(path: Path, max_age_seconds: int) -> dict[str, Any]:
    evidence = read_json(path)
    if evidence.get("schema_version") != 1:
        raise BatchStop("global_status_source_failure", "unsupported status evidence schema")
    unavailable = list(evidence.get("unavailable_sources") or []) + list(
        evidence.get("unavailable_hosts") or []
    )
    if evidence.get("global_complete") is not True or unavailable:
        raise BatchStop(
            "global_status_source_failure",
            {"global_complete": evidence.get("global_complete"), "unavailable": unavailable},
        )
    captured = parse_time(evidence.get("captured_at"), "captured_at")
    age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise BatchStop("global_status_source_failure", {"status_evidence_age_seconds": age})
    threads = evidence.get("threads")
    if not isinstance(threads, dict):
        raise BatchStop("global_status_source_failure", "threads map is missing")
    return evidence


def run_tool(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise BatchStop(
            label,
            {
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            },
        )
    return completed


def run_preflight(codex_home: Path, output: Path) -> dict[str, Any]:
    script = Path(__file__).with_name("subagent_delete_compat.py")
    run_tool(
        [
            sys.executable,
            str(script),
            "preflight",
            "--codex-home",
            str(codex_home),
            "--output",
            str(output),
        ],
        "runtime_mismatch",
    )
    report = read_json(output)
    if (
        report.get("decision") != "canary_required"
        or report.get("allow_expensive_inventory") is not True
        or report.get("native_delete") is not True
        or not report.get("recommended_codex_exe")
    ):
        raise BatchStop(
            "runtime_mismatch",
            {
                "decision": report.get("decision"),
                "condition_key": report.get("condition_key"),
                "reasons": report.get("reasons"),
            },
        )
    return report


def fresh_enough_preflight(path: Path, codex_home: Path, max_age_seconds: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    report = read_json(path)
    try:
        checked = parse_time(report.get("checked_at"), "preflight checked_at")
    except BatchStop:
        return None
    age = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
    if age < -60 or age > max_age_seconds:
        return None
    try:
        same_home = Path(str(report.get("codex_home"))).resolve(strict=True) == codex_home
    except OSError:
        same_home = False
    if (
        not same_home
        or report.get("decision") != "canary_required"
        or report.get("allow_expensive_inventory") is not True
        or report.get("native_delete") is not True
        or not report.get("recommended_codex_exe")
    ):
        return None
    return report


def online_backup(codex_home: Path, backup_dir: Path, summary_path: Path) -> dict[str, Any]:
    if is_below(backup_dir, codex_home):
        raise BatchStop("backup_inside_codex_home", str(backup_dir))
    backup_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    for name in DB_NAMES:
        source = codex_home / name
        destination = backup_dir / name
        source_check = quick_check(source)
        if source_check != "ok":
            raise BatchStop("database_error", {"database": name, "quick_check": source_check})
        try:
            with closing(readonly_connection(source)) as source_connection:
                with closing(sqlite3.connect(destination)) as destination_connection:
                    source_connection.backup(destination_connection, pages=1024, sleep=0.05)
        except sqlite3.Error as exc:
            raise BatchStop("database_error", {"database": name, "error": str(exc)}) from exc
        metadata = os.lstat(destination)
        if destination.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE:
            raise BatchStop("invalid_backup_path", str(destination))
        if int(getattr(metadata, "st_nlink", 1)) != 1:
            raise BatchStop("invalid_backup_hardlink", str(destination))
        backup_check = quick_check(destination)
        if backup_check != "ok":
            raise BatchStop("database_error", {"backup": str(destination), "quick_check": backup_check})
        rows.append(
            {
                "database": name,
                "source": str(source),
                "source_bytes": source.stat().st_size,
                "source_quick_check": source_check,
                "backup": str(destination),
                "backup_bytes": destination.stat().st_size,
                "backup_quick_check": backup_check,
                "backup_sha256": sha256_file(destination),
            }
        )
    summary = {
        "schema_version": 1,
        "created_at": now_iso(),
        "codex_home": str(codex_home),
        "backup_dir": str(backup_dir),
        "databases": rows,
    }
    write_json_atomic(summary_path, summary)
    return summary


def light_database_fingerprint(codex_home: Path) -> dict[str, Any]:
    try:
        with closing(readonly_connection(codex_home / "state_5.sqlite")) as connection:
            history = [
                {
                    "version": int(row[0]),
                    "description": str(row[1]),
                    "success": int(row[2]),
                    "checksum_hex": str(row[3]).upper(),
                }
                for row in connection.execute(
                    "SELECT version, description, success, hex(checksum) "
                    "FROM _sqlx_migrations ORDER BY version"
                )
            ]
            return {
                "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
                "migration_history_sha256": canonical_hash(history),
            }
    except sqlite3.Error as exc:
        raise BatchStop("database_error", str(exc)) from exc


def process_exists(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, process_id
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(process_id, 0)
        return True
    except OSError:
        return False


def make_light_fingerprint(runtime: dict[str, Any], protected: set[str]) -> dict[str, Any]:
    process = runtime["runtime"]["desktop_process"]
    mirror = runtime["runtime"]["mirror"]
    return {
        "condition_key": runtime["condition_key"],
        "desktop_process_id": int(process["process_id"]),
        "recommended_codex_exe": str(runtime["recommended_codex_exe"]),
        "mirror_bytes": int(mirror["bytes"]),
        "mirror_mtime_ns": int(mirror["mtime_ns"]),
        "schema_version": int(runtime["database"]["schema_version"]),
        "migration_history_sha256": str(runtime["database"]["migration_history_sha256"]),
        "protected_ids_sha256": canonical_hash(sorted(protected)),
    }


def check_light_fingerprint(
    expected: dict[str, Any], codex_home: Path, protected: set[str]
) -> dict[str, Any]:
    if canonical_hash(sorted(protected)) != expected["protected_ids_sha256"]:
        raise BatchStop("global_status_source_failure", "monotonic protection set changed")
    process_id = int(expected["desktop_process_id"])
    if not process_exists(process_id):
        raise BatchStop("runtime_mismatch", {"missing_process_id": process_id})
    executable = Path(str(expected["recommended_codex_exe"])).resolve(strict=True)
    metadata = executable.stat()
    if (
        metadata.st_size != int(expected["mirror_bytes"])
        or metadata.st_mtime_ns != int(expected["mirror_mtime_ns"])
    ):
        raise BatchStop("runtime_mismatch", "recommended runtime file changed")
    database = light_database_fingerprint(codex_home)
    if (
        database["schema_version"] != expected["schema_version"]
        or database["migration_history_sha256"] != expected["migration_history_sha256"]
    ):
        raise BatchStop("runtime_mismatch", {"database_fingerprint": database})
    return {
        "condition_key": expected["condition_key"],
        "desktop_process_id": process_id,
        "database": database,
        "protected_ids_sha256": expected["protected_ids_sha256"],
    }


def build_plan(
    manifest: dict[str, Any],
    status: dict[str, Any],
    codex_home: Path,
    protected: set[str],
    min_idle_seconds: int,
) -> dict[str, Any]:
    candidates = {str(item["id"]): item for item in manifest.get("candidates", [])}
    status_threads = status["threads"]
    rows, children, edge_statuses = load_state(codex_home)
    roots: list[dict[str, Any]] = []
    initial_skips: list[dict[str, Any]] = []
    now_ns = time.time_ns()
    for source_root in manifest.get("candidate_roots", []):
        root_id = str(source_root["root_id"])
        expected_ids = [str(item) for item in source_root.get("thread_ids", [])]
        live_ids = subtree(root_id, children)
        reasons: list[str] = []
        if set(live_ids) != set(expected_ids):
            reasons.append("state_changed")
        overlap = sorted(set(expected_ids) & protected)
        if overlap:
            reasons.append("protected")
        files: list[dict[str, Any]] = []
        row_snapshots: list[dict[str, Any]] = []
        for thread_id in expected_ids:
            candidate = candidates.get(thread_id)
            row = rows.get(thread_id)
            live_status = status_threads.get(thread_id)
            if not candidate or not candidate.get("reasons"):
                reasons.append("weak_subagent_evidence")
                continue
            if candidate.get("archived") or (row and bool(row.get("archived"))):
                reasons.append("archived")
            if not isinstance(live_status, dict) or status_value(live_status) not in COMPLETED_STATUSES:
                reasons.append("status_not_completed")
            if row is None:
                reasons.append("state_changed")
                continue
            try:
                snap = file_snapshot(str(row.get("rollout_path") or ""), codex_home)
            except RootSkip:
                reasons.append("rollout_changed")
                continue
            files.append({"thread_id": thread_id, **snap})
            row_snapshots.append(
                {
                    "thread_id": thread_id,
                    "updated_at": row.get("updated_at"),
                    "updated_at_ms": row.get("updated_at_ms"),
                    "archived": int(row.get("archived") or 0),
                    "edge_status": edge_statuses.get(thread_id, ""),
                }
            )
        if files:
            newest_ns = max(int(item["mtime_ns"]) for item in files)
            if now_ns - newest_ns < min_idle_seconds * 1_000_000_000:
                reasons.append("recently_changed")
        if reasons:
            initial_skips.append(
                {
                    "root_id": root_id,
                    "thread_ids": expected_ids,
                    "size_bytes": int(source_root.get("size_bytes") or 0),
                    "outcome": "skipped",
                    "reason": sorted(set(reasons)),
                    "recorded_at": now_iso(),
                }
            )
            continue
        roots.append(
            {
                "root_id": root_id,
                "thread_ids": expected_ids,
                "thread_count": len(expected_ids),
                "size_bytes": sum(int(item["size_bytes"]) for item in files),
                "files": files,
                "rows": row_snapshots,
            }
        )
    roots.sort(key=lambda item: (int(item["size_bytes"]), str(item["root_id"])))
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "codex_home": str(codex_home),
        "source_manifest": manifest,
        "status_evidence_captured_at": status["captured_at"],
        "protected_ids": sorted(protected),
        "roots": roots,
        "initial_skips": initial_skips,
        "eligible_root_count": len(roots),
        "eligible_thread_count": sum(int(item["thread_count"]) for item in roots),
        "eligible_bytes": sum(int(item["size_bytes"]) for item in roots),
    }


def writer_lock_members(plan_root: dict[str, Any], codex_home: Path) -> list[str]:
    lock_dir = codex_home / "thread-writer-locks"
    return [
        thread_id
        for thread_id in plan_root["thread_ids"]
        if (lock_dir / f"{thread_id}.lock").exists()
    ]


def inspect_root_presence(plan_root: dict[str, Any], codex_home: Path) -> dict[str, Any]:
    expected = [str(item) for item in plan_root["thread_ids"]]
    rows, children, _ = load_state(codex_home)
    present_rows = sorted(item for item in expected if item in rows)
    live_subtree = subtree(str(plan_root["root_id"]), children) if str(plan_root["root_id"]) in rows else []
    present_files = sorted(
        str(item["thread_id"])
        for item in plan_root["files"]
        if Path(str(item["path"])).exists()
    )
    all_present = set(present_rows) == set(expected) and set(present_files) == set(expected)
    all_absent = not present_rows and not present_files and not (set(live_subtree) & set(expected))
    return {
        "present_rows": present_rows,
        "present_files": present_files,
        "live_subtree": live_subtree,
        "all_present": all_present,
        "all_absent": all_absent,
    }


def precheck_root(plan_root: dict[str, Any], codex_home: Path, protected: set[str]) -> dict[str, Any]:
    root_id = str(plan_root["root_id"])
    presence = inspect_root_presence(plan_root, codex_home)
    if presence["all_absent"]:
        raise RootSkip("already_absent", presence)
    if not presence["all_present"]:
        raise PartialDeletion("partial_deletion", presence)
    rows, children, edge_statuses = load_state(codex_home)
    expected = [str(item) for item in plan_root["thread_ids"]]
    live = subtree(root_id, children) if root_id in rows else []
    if set(live) != set(expected):
        raise RootSkip("state_changed", {"expected": sorted(expected), "live": sorted(live)})
    overlap = sorted(set(live) & protected)
    if overlap:
        raise RootSkip("protected", overlap)
    expected_rows = {str(item["thread_id"]): item for item in plan_root["rows"]}
    expected_files = {str(item["thread_id"]): item for item in plan_root["files"]}
    for thread_id in live:
        row = rows.get(thread_id)
        if row is None:
            raise RootSkip("state_changed", thread_id)
        row_snapshot = expected_rows[thread_id]
        if (
            row.get("updated_at") != row_snapshot.get("updated_at")
            or row.get("updated_at_ms") != row_snapshot.get("updated_at_ms")
            or int(row.get("archived") or 0) != int(row_snapshot.get("archived") or 0)
            or edge_statuses.get(thread_id, "") != row_snapshot.get("edge_status", "")
        ):
            raise RootSkip("state_changed", thread_id)
        current = file_snapshot(str(row.get("rollout_path") or ""), codex_home)
        expected_file = expected_files[thread_id]
        if current["path"].casefold() != str(expected_file["path"]).casefold():
            raise RootSkip("rollout_path_changed", thread_id)
        if current["size_bytes"] != int(expected_file["size_bytes"]):
            raise RootSkip("rollout_size_changed", thread_id)
        if current["mtime_ns"] != int(expected_file["mtime_ns"]):
            raise RootSkip("rollout_mtime_changed", thread_id)
    locks = writer_lock_members(plan_root, codex_home)
    if locks:
        raise RootSkip("writer_lock", locks)
    return {"root_id": root_id, "thread_ids": live}


def verify_deleted(plan_root: dict[str, Any], codex_home: Path) -> dict[str, Any]:
    presence = inspect_root_presence(plan_root, codex_home)
    check = quick_check(codex_home / "state_5.sqlite")
    return {**presence, "state_quick_check": check, "ok": presence["all_absent"] and check == "ok"}


def active_writer_error(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    try:
        code = int(error.get("code", 0))
    except (TypeError, ValueError):
        return False
    return code == ACTIVE_WRITER_CODE and ACTIVE_WRITER_TEXT in str(
        error.get("message", "")
    ).casefold()


class AppServer:
    def __init__(self, executable: Path, codex_home: Path, timeout: float) -> None:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        self.timeout = timeout
        self.process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
        self.stdout_queue: queue.Queue[str] = queue.Queue()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        response = self.rpc(
            1,
            "initialize",
            {
                "clientInfo": {"name": "codex-storage-cleanup", "version": "2.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        if "error" in response:
            raise BatchStop("runtime_mismatch", response["error"])

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout_queue.put(line)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip())
            if len(self.stderr_lines) > 200:
                del self.stderr_lines[:50]

    def rpc(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise BatchStop(
                "unknown_rpc_error",
                {"exit_code": self.process.returncode, "stderr": self.stderr_lines[-20:]},
            )
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":"))
            + "\n"
        )
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                line = self.stdout_queue.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == request_id:
                return payload
        raise BatchStop("unknown_rpc_error", {"method": method, "reason": "timeout"})

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            assert self.process.stdin is not None
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def new_results(plan_path: Path, plan: dict[str, Any], fingerprint: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "plan": str(plan_path),
        "condition_key": fingerprint["condition_key"],
        "protected_ids": sorted(plan.get("protected_ids", [])),
        "protected_ids_sha256": fingerprint["protected_ids_sha256"],
        "entries": list(plan.get("initial_skips", [])),
        "stop": None,
        "summary": None,
    }


def record_entry(results_path: Path, results: dict[str, Any], entry: dict[str, Any]) -> None:
    results["entries"].append(entry)
    write_json_atomic(results_path, results)


def progress(results: dict[str, Any], total: int, *, force: bool = False) -> None:
    handled = len({str(item["root_id"]) for item in results["entries"]})
    if not force and handled % 10:
        return
    deleted = sum(1 for item in results["entries"] if item.get("outcome") == "deleted")
    skipped = sum(1 for item in results["entries"] if item.get("outcome") == "skipped")
    released = sum(
        int(item.get("size_bytes") or 0)
        for item in results["entries"]
        if item.get("outcome") == "deleted"
    )
    print(
        json.dumps(
            {
                "progress": f"{handled}/{total}",
                "deleted_roots": deleted,
                "skipped_roots": skipped,
                "released_bytes": released,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def execute_roots(
    plan: dict[str, Any],
    plan_path: Path,
    results_path: Path,
    codex_home: Path,
    protected: set[str],
    fingerprint: dict[str, Any],
    executable: Path,
    timeout: float,
    *,
    server_factory: Callable[[Path, Path, float], Any] = AppServer,
) -> dict[str, Any]:
    if results_path.exists():
        results = read_json(results_path)
        if results.get("condition_key") != fingerprint["condition_key"]:
            raise BatchStop("runtime_mismatch", "resume condition key changed")
        previous_protected = {str(item).casefold() for item in results.get("protected_ids", [])}
        if not previous_protected.issubset(protected):
            raise BatchStop("global_status_source_failure", "resume protection set shrank")
        if previous_protected != protected:
            results["protected_ids"] = sorted(protected)
            results["protected_ids_sha256"] = fingerprint["protected_ids_sha256"]
            write_json_atomic(results_path, results)
    else:
        results = new_results(plan_path, plan, fingerprint)
        write_json_atomic(results_path, results)
    handled = {str(item["root_id"]) for item in results["entries"]}
    pending = [item for item in plan["roots"] if str(item["root_id"]) not in handled]
    total = len(plan["roots"]) + len(plan.get("initial_skips", []))
    if not pending:
        progress(results, total, force=True)
        return results
    server = server_factory(executable, codex_home, timeout)
    request_id = 100
    canary_done = any(item.get("canary") for item in results["entries"])
    try:
        for root in pending:
            root_id = str(root["root_id"])
            try:
                lightweight = check_light_fingerprint(fingerprint, codex_home, protected)
                precheck = precheck_root(root, codex_home, protected)
                response = server.rpc(request_id, "thread/delete", {"threadId": root_id})
                request_id += 1
                if "error" in response:
                    if not active_writer_error(response["error"]):
                        raise BatchStop("unknown_rpc_error", {"root_id": root_id, "error": response["error"]})
                    presence = inspect_root_presence(root, codex_home)
                    if not presence["all_present"]:
                        raise PartialDeletion(
                            "partial_deletion",
                            {"root_id": root_id, "error": response["error"], "presence": presence},
                        )
                    raise RootSkip("active_writer", response["error"])
                verification = verify_deleted(root, codex_home)
                if not verification["ok"]:
                    raise PartialDeletion(
                        "partial_deletion", {"root_id": root_id, "verification": verification}
                    )
                entry = {
                    "root_id": root_id,
                    "thread_ids": root["thread_ids"],
                    "size_bytes": root["size_bytes"],
                    "outcome": "deleted",
                    "canary": not canary_done,
                    "method": "native app-server thread/delete",
                    "recorded_at": now_iso(),
                    "lightweight_fingerprint": lightweight,
                    "precheck": precheck,
                    "response": response,
                    "verification": verification,
                }
                canary_done = True
                record_entry(results_path, results, entry)
            except RootSkip as exc:
                record_entry(
                    results_path,
                    results,
                    {
                        "root_id": root_id,
                        "thread_ids": root["thread_ids"],
                        "size_bytes": root["size_bytes"],
                        "outcome": "skipped",
                        "reason": exc.reason,
                        "detail": exc.detail,
                        "recorded_at": now_iso(),
                    },
                )
            progress(results, total)
    finally:
        server.close()
    progress(results, total, force=True)
    return results


def final_verify(
    plan: dict[str, Any], results: dict[str, Any], codex_home: Path, protected: set[str]
) -> dict[str, Any]:
    roots = {str(item["root_id"]): item for item in plan["roots"]}
    root_checks: list[dict[str, Any]] = []
    skipped_root_checks: list[dict[str, Any]] = []
    for entry in results["entries"]:
        root = roots.get(str(entry["root_id"]))
        if not root:
            continue
        if entry.get("outcome") == "deleted":
            root_checks.append({"root_id": entry["root_id"], **verify_deleted(root, codex_home)})
        elif entry.get("outcome") == "skipped":
            presence = inspect_root_presence(root, codex_home)
            skipped_root_checks.append(
                {
                    "root_id": entry["root_id"],
                    **presence,
                    "ok": presence["all_present"] or presence["all_absent"],
                }
            )
    rows, _, _ = load_state(codex_home)
    protected_checks: list[dict[str, Any]] = []
    for thread_id in sorted(protected):
        row = rows.get(thread_id)
        rollout_exists = False
        if row and row.get("rollout_path"):
            try:
                rollout_exists = normalize_rollout(
                    str(row["rollout_path"]), codex_home
                ).is_file()
            except (RootSkip, BatchStop):
                rollout_exists = False
        protected_checks.append(
            {"id": thread_id, "row_exists": row is not None, "rollout_exists": rollout_exists}
        )
    database_checks = [
        {
            "database": name,
            "quick_check": quick_check(codex_home / name),
            "bytes": (codex_home / name).stat().st_size,
        }
        for name in CHECK_DB_NAMES
    ]
    ok = (
        all(item["ok"] for item in root_checks)
        and all(item["ok"] for item in skipped_root_checks)
        and all(item["row_exists"] and item["rollout_exists"] for item in protected_checks)
        and all(item["quick_check"] == "ok" for item in database_checks)
    )
    return {
        "schema_version": 1,
        "verified_at": now_iso(),
        "ok": ok,
        "deleted_root_checks": root_checks,
        "skipped_root_checks": skipped_root_checks,
        "protected": protected_checks,
        "database_checks": database_checks,
    }


def summarize(results: dict[str, Any]) -> dict[str, Any]:
    entries = results["entries"]
    return {
        "deleted_roots": sum(1 for item in entries if item.get("outcome") == "deleted"),
        "deleted_threads": sum(
            len(item.get("thread_ids") or []) for item in entries if item.get("outcome") == "deleted"
        ),
        "skipped_roots": sum(1 for item in entries if item.get("outcome") == "skipped"),
        "failed_roots": 1 if results.get("stop") else 0,
        "released_bytes": sum(
            int(item.get("size_bytes") or 0)
            for item in entries
            if item.get("outcome") == "deleted"
        ),
        "skip_reasons": sorted(
            {
                str(item.get("reason"))
                for item in entries
                if item.get("outcome") == "skipped"
            }
        ),
    }


def cmd_inventory(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve(strict=True)
    run_dir = require_external_run_dir(args.run_dir, codex_home, create=True)
    preflight_path = run_dir / "runtime-preflight-inventory.json"
    report = run_preflight(codex_home, preflight_path)
    manifest_path = run_dir / "subagent-manifest.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("subagent_inventory.py")),
        "--codex-home",
        str(codex_home),
        "--output",
        str(manifest_path),
        "--overwrite",
    ]
    for thread_id in args.protect:
        command.extend(("--protect", thread_id))
    run_tool(command, "inventory_failure")
    manifest = read_json(manifest_path)
    targets = [str(item["id"]) for item in manifest.get("candidates", [])]
    write_json_atomic(
        run_dir / "status-targets.json",
        {"schema_version": 1, "created_at": now_iso(), "thread_ids": targets},
    )
    print(
        json.dumps(
            {
                "condition_key": report["condition_key"],
                "candidate_roots": manifest.get("candidate_root_count"),
                "candidate_threads": manifest.get("candidate_count"),
                "candidate_bytes": manifest.get("candidate_bytes"),
                "manifest": str(manifest_path),
                "status_targets": str(run_dir / "status-targets.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve(strict=True)
    run_dir = require_external_run_dir(args.run_dir, codex_home, create=False)
    manifest = read_json(run_dir / "subagent-manifest.json")
    status_path = Path(args.status_evidence).resolve(strict=True)
    if is_below(status_path, codex_home):
        raise BatchStop("global_status_source_failure", "status evidence must be external")
    status = validate_status_evidence(status_path, args.status_max_age_seconds)
    protected = {
        str(item).casefold()
        for item in [
            *manifest.get("protected_ids", []),
            *status.get("protected_ids", []),
            *args.protect,
        ]
        if item
    }
    plan_path = run_dir / "deletion-plan.json"
    if plan_path.exists() and args.resume:
        plan = read_json(plan_path)
        protected |= {str(item).casefold() for item in plan.get("protected_ids", [])}
        plan["protected_ids"] = sorted(protected)
        write_json_atomic(plan_path, plan)
    elif plan_path.exists():
        raise BatchStop("existing_plan_requires_resume", str(plan_path))
    else:
        plan = build_plan(manifest, status, codex_home, protected, args.min_idle_seconds)
        write_json_atomic(plan_path, plan)
    if not plan["roots"]:
        results = new_results(plan_path, plan, {"condition_key": "none", "protected_ids_sha256": canonical_hash(sorted(protected))})
        results["summary"] = summarize(results)
        write_json_atomic(run_dir / "deletion-results.json", results)
        print(json.dumps(results["summary"], ensure_ascii=False))
        return 0

    inventory_preflight_path = run_dir / "runtime-preflight-inventory.json"
    batch_runtime = fresh_enough_preflight(
        inventory_preflight_path, codex_home, args.status_max_age_seconds
    )
    if batch_runtime is None:
        batch_preflight_path = run_dir / f"runtime-preflight-batch-{timestamp_slug()}.json"
        batch_runtime = run_preflight(codex_home, batch_preflight_path)
    backup_dir = run_dir / f"db-backup-{timestamp_slug()}"
    backup_summary_path = run_dir / f"db-backup-summary-{timestamp_slug()}.json"
    online_backup(codex_home, backup_dir, backup_summary_path)
    canary_preflight_path = run_dir / f"runtime-preflight-canary-{timestamp_slug()}.json"
    canary_runtime = run_preflight(codex_home, canary_preflight_path)
    if canary_runtime["condition_key"] != batch_runtime["condition_key"]:
        raise BatchStop(
            "runtime_mismatch",
            {
                "batch_condition_key": batch_runtime["condition_key"],
                "canary_condition_key": canary_runtime["condition_key"],
            },
        )
    fingerprint = make_light_fingerprint(canary_runtime, protected)
    results_path = run_dir / "deletion-results.json"
    try:
        results = execute_roots(
            plan,
            plan_path,
            results_path,
            codex_home,
            protected,
            fingerprint,
            Path(str(canary_runtime["recommended_codex_exe"])).resolve(strict=True),
            args.timeout,
        )
        verification = final_verify(plan, results, codex_home, protected)
        write_json_atomic(run_dir / "final-verification.json", verification)
        if not verification["ok"]:
            if any(not item["ok"] for item in verification["deleted_root_checks"]):
                raise PartialDeletion("partial_deletion", "deleted root failed final verification")
            if any(not item["ok"] for item in verification["skipped_root_checks"]):
                raise PartialDeletion("partial_deletion", "skipped root became partial")
            raise BatchStop("database_error", "final verification failed")
        results["summary"] = summarize(results)
        write_json_atomic(results_path, results)
        print(json.dumps(results["summary"], ensure_ascii=False))
        return 0
    except BatchStop as exc:
        results = read_json(results_path) if results_path.exists() else new_results(plan_path, plan, fingerprint)
        results["stop"] = {
            "reason": exc.reason,
            "detail": exc.detail,
            "recorded_at": now_iso(),
        }
        results["summary"] = summarize(results)
        write_json_atomic(results_path, results)
        print(json.dumps({"stop": results["stop"], "summary": results["summary"]}, ensure_ascii=False))
        return 2


def cmd_verify(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).resolve(strict=True)
    run_dir = require_external_run_dir(args.run_dir, codex_home, create=False)
    plan = read_json(run_dir / "deletion-plan.json")
    results = read_json(run_dir / "deletion-results.json")
    protected = {str(item).casefold() for item in plan.get("protected_ids", [])}
    verification = final_verify(plan, results, codex_home, protected)
    write_json_atomic(run_dir / "final-verification.json", verification)
    print(json.dumps({"ok": verification["ok"], "summary": summarize(results)}, ensure_ascii=False))
    return 0 if verification["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Run preflight and strong-evidence inventory")
    inventory.add_argument("--codex-home", default=r"D:\CodexHome")
    inventory.add_argument("--run-dir", required=True)
    inventory.add_argument("--protect", action="append", default=[])
    inventory.set_defaults(func=cmd_inventory)

    run = subparsers.add_parser("run", help="Back up, canary, delete, continue, and verify")
    run.add_argument("--codex-home", default=r"D:\CodexHome")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--status-evidence", required=True)
    run.add_argument("--protect", action="append", default=[])
    run.add_argument("--status-max-age-seconds", type=int, default=DEFAULT_STATUS_MAX_AGE_SECONDS)
    run.add_argument("--min-idle-seconds", type=int, default=DEFAULT_MIN_IDLE_SECONDS)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--resume", action="store_true")
    run.set_defaults(func=cmd_run)

    verify = subparsers.add_parser("verify", help="Repeat final read-only verification")
    verify.add_argument("--codex-home", default=r"D:\CodexHome")
    verify.add_argument("--run-dir", required=True)
    verify.set_defaults(func=cmd_verify)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except BatchStop as exc:
        print(
            json.dumps({"stop": exc.reason, "detail": exc.detail}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"stop": "unexpected_internal_error", "error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
