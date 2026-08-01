#!/usr/bin/env python3
"""Diagnose and manage one narrowly verified Codex delete compatibility shim.

The default and ``diagnose`` paths are read-only. ``install`` and ``remove``
require exact profile matches, explicit execution flags, confirmation tokens,
and audit output outside CodexHome. This module never invokes a delete API.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT / "references" / "subagent-delete-compatibility.json"
TRUSTED_REMOTE = "https://github.com/skills-qweer/codex-storage-cleanup.git"
KNOWN_OBJECTS = {
    "agent_jobs",
    "agent_job_items",
    "idx_agent_jobs_status",
    "idx_agent_job_items_status",
}
REVIEWED_OBJECT_SPECS = {
    "agent_jobs": (
        "table",
        "ebc00df15de0bac3fb0b0e807653b61d7f1589cf1dc2dc3f7ff150ebdeaae195",
    ),
    "agent_job_items": (
        "table",
        "53eeaf3f0c6d72ba3a45b135d79e58aa73c60de23b27fa93c35b5b2ca4c4a872",
    ),
    "idx_agent_jobs_status": (
        "index",
        "895f9a3fba66a2b807f3b29d7838b41cfd53734481e66c192ae0ad50b9a47fc6",
    ),
    "idx_agent_job_items_status": (
        "index",
        "17ac9763c095bcbe1c80dc8967cb22b791da86202acbab99bc29b73d60007b85",
    ),
}
REVIEWED_PROFILE_ID = "codex-0.142.2-state-migration-42-missing-agent-jobs-v1"
REVIEWED_MIGRATIONS = {
    14: (
        "agent jobs",
        "12275BDF6BD1685525DBB54C37DDF62608D10F9F66110D7BB0DE55EE25A8B283E46D97CB8EABED7564F9752E3967BA8A",
    ),
    15: (
        "agent jobs max runtime seconds",
        "8104857BCB63E9665C77DDCB8186BE1BB630D9472FE215A0FADD62566FE33ABDA9CB223602506F2187F2BDD09D01105E",
    ),
    42: (
        "drop agent jobs",
        "815A1F0CBE21AC7F0653FB67C8E9702FD0EBB5F0A54CE644893B66D18614D9C8988510458B68FBD671B8632EF363B36A",
    ),
}
REQUIRED_BACKUPS = {"state_5.sqlite", "goals_1.sqlite", "memories_1.sqlite"}
INSTALL_TOKEN = "INSTALL_AGENT_JOBS_COMPAT_0142"
REMOVE_TOKEN = "REMOVE_AGENT_JOBS_COMPAT_0142"
DECISION_EXIT_CODES = {
    "canary_required": 0,
    "known_workaround_eligible": 0,
    "compat_installed": 0,
    "stale_profile_update_required": 3,
    "unsupported_update_required": 3,
    "unsafe_stop": 4,
}


class SafetyError(RuntimeError):
    """Raised when a safety precondition is not met."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_identity(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "resolved_path": str(path),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
    }


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).casefold()


def sql_hash(sql: str) -> str:
    return sha256_bytes(normalize_sql(sql).encode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyError(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SafetyError(f"Expected a JSON object in {path}")
    return value


def parse_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SafetyError(f"Invalid {label}: {value!r}") from exc


def parse_datetime(value: str, label: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SafetyError(f"Invalid {label}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SafetyError(f"{label} must include a timezone: {value!r}")
    return parsed.astimezone(dt.timezone.utc)


def canonical(path: Path, *, must_exist: bool = True) -> Path:
    try:
        return path.expanduser().resolve(strict=must_exist)
    except OSError as exc:
        raise SafetyError(f"Cannot resolve path {path}: {exc}") from exc


def is_below(path: Path, parent: Path) -> bool:
    path_text = os.path.normcase(str(path))
    parent_text = os.path.normcase(str(parent))
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def ensure_external_path(path: Path, codex_home: Path, *, must_exist: bool) -> Path:
    resolved = canonical(path, must_exist=must_exist)
    if is_below(resolved, codex_home):
        raise SafetyError(f"Audit and backup paths must be outside CodexHome: {resolved}")
    return resolved


def require_repository_profile(path: Path) -> Path:
    supplied_absolute = Path(os.path.abspath(path))
    expected_absolute = Path(os.path.abspath(DEFAULT_PROFILE))
    if os.path.normcase(str(supplied_absolute)) != os.path.normcase(str(expected_absolute)):
        raise SafetyError(
            f"Live compatibility writes require the reviewed repository profile: {expected_absolute}"
        )
    try:
        metadata = expected_absolute.lstat()
    except OSError as exc:
        raise SafetyError(f"Cannot inspect bundled compatibility profile: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if expected_absolute.is_symlink() or int(
        getattr(metadata, "st_file_attributes", 0)
    ) & reparse_flag:
        raise SafetyError("Bundled compatibility profile must not be a symlink or reparse point")
    resolved = canonical(expected_absolute)
    if resolved.parent != canonical(REPO_ROOT / "references"):
        raise SafetyError("Bundled compatibility profile escaped the repository references directory")

    def git_output(arguments: list[str], *, binary: bool = False) -> bytes | str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=not binary,
            timeout=15,
        )
        if result.returncode != 0:
            detail = result.stderr if binary else (result.stderr or result.stdout)
            raise SafetyError(f"Cannot verify trusted skill repository: {detail!r}")
        return result.stdout

    if str(git_output(["branch", "--show-current"])).strip() != "main":
        raise SafetyError("Live compatibility writes require the reviewed main branch")
    if str(
        git_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    ).strip() != "origin/main":
        raise SafetyError("Live compatibility writes require main to track origin/main")
    if str(git_output(["remote", "get-url", "origin"])).strip() != TRUSTED_REMOTE:
        raise SafetyError("Live compatibility writes require the allow-listed HTTPS origin")
    status = str(git_output(["status", "--porcelain=v1", "--untracked-files=all"])).strip()
    if status:
        raise SafetyError("Live compatibility writes require a completely clean skill repository")
    head = str(git_output(["rev-parse", "HEAD"])).strip()
    remote_head = str(git_output(["rev-parse", "origin/main"])).strip()
    if head != remote_head:
        raise SafetyError("Live compatibility writes require HEAD to equal the reviewed origin/main")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        marker_result = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", marker],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if marker_result.returncode == 0:
            raise SafetyError(f"Live compatibility writes refuse an active Git state: {marker}")
    tracked = git_output(
        ["show", "HEAD:references/subagent-delete-compatibility.json"], binary=True
    )
    assert isinstance(tracked, bytes)
    working = expected_absolute.read_bytes()
    if tracked.replace(b"\r\n", b"\n") != working.replace(b"\r\n", b"\n"):
        raise SafetyError("Bundled profile content differs from the reviewed HEAD blob")
    return resolved


def require_live_state_database(codex_home: Path, name: str) -> Path:
    if name != "state_5.sqlite":
        raise SafetyError(f"Live compatibility writes only support state_5.sqlite, not {name!r}")
    candidate = codex_home / name
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise SafetyError(f"Cannot inspect live state database {candidate}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = int(getattr(metadata, "st_file_attributes", 0))
    if candidate.is_symlink() or file_attributes & reparse_flag:
        raise SafetyError(f"Live state database must not be a symlink or reparse point: {candidate}")
    resolved = canonical(candidate)
    if resolved.parent != codex_home:
        raise SafetyError(f"Live state database escaped CodexHome: {resolved}")
    return resolved


def readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path).replace(os.sep, '/'), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 2000")
    return connection


def quick_check_path(path: Path) -> str:
    connection = readonly_connection(path)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else "missing-result"


def validate_profile_document(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("schema_version") != 1:
        raise SafetyError("Unsupported compatibility profile schema_version")
    parse_date(document.get("updated_at"), "profile updated_at")
    parse_date(document.get("review_after"), "profile review_after")
    policy = document.get("policy")
    if not isinstance(policy, dict):
        raise SafetyError("Compatibility document policy is missing")
    for key in (
        "backup_max_age_minutes",
        "failure_evidence_max_age_hours",
        "compat_install_max_age_hours",
    ):
        if not isinstance(policy.get(key), int) or int(policy[key]) <= 0:
            raise SafetyError(f"Compatibility policy {key} must be a positive integer")
    if (
        policy["backup_max_age_minutes"] != 60
        or policy["failure_evidence_max_age_hours"] != 24
        or policy["compat_install_max_age_hours"] != 24
        or policy.get("version_alone_is_never_sufficient") is not True
        or policy.get("automatic_database_workaround") is not False
        or policy.get("automatic_repository_merge") is not False
    ):
        raise SafetyError("Critical compatibility policy differs from the reviewed limits")
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != 1:
        raise SafetyError("This script supports exactly one reviewed compatibility profile")

    seen_ids: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise SafetyError("Each compatibility profile must be an object")
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not profile_id or profile_id in seen_ids:
            raise SafetyError(f"Invalid or duplicate profile id: {profile_id!r}")
        seen_ids.add(profile_id)
        if profile_id != REVIEWED_PROFILE_ID:
            raise SafetyError(f"Unreviewed compatibility profile id: {profile_id}")
        if profile.get("status") != "manual_compat_only":
            raise SafetyError(f"Unsupported profile status in {profile_id}")
        if profile.get("state_database") != "state_5.sqlite":
            raise SafetyError(f"Profile {profile_id} targets an unsupported database")
        versions = profile.get("codex_cli_versions")
        if not isinstance(versions, list) or not versions or not all(
            isinstance(item, str) and re.fullmatch(r"\d+\.\d+\.\d+", item)
            for item in versions
        ):
            raise SafetyError(f"Invalid codex_cli_versions in {profile_id}")
        if versions != ["0.142.2"]:
            raise SafetyError(f"Profile {profile_id} broadens the reviewed CLI version")
        parse_date(profile.get("validated_at"), f"{profile_id} validated_at")
        parse_date(profile.get("review_after"), f"{profile_id} review_after")

        migration = profile.get("migration")
        if not isinstance(migration, dict) or not isinstance(
            migration.get("max_successful"), int
        ):
            raise SafetyError(f"Invalid migration rule in {profile_id}")
        if migration["max_successful"] != 42:
            raise SafetyError(f"Profile {profile_id} broadens the reviewed migration maximum")
        required = migration.get("required")
        if not isinstance(required, list) or not required:
            raise SafetyError(f"Missing required migrations in {profile_id}")
        for item in required:
            if not isinstance(item, dict) or not isinstance(item.get("version"), int):
                raise SafetyError(f"Invalid required migration in {profile_id}")
            checksum = item.get("checksum_hex")
            if not isinstance(checksum, str) or not re.fullmatch(
                r"[0-9A-F]{96}", checksum
            ):
                raise SafetyError(f"Invalid migration checksum in {profile_id}")
        required_by_version = {item["version"]: item for item in required}
        if set(required_by_version) != set(REVIEWED_MIGRATIONS):
            raise SafetyError(f"Profile {profile_id} changes the reviewed migration set")
        for version_number, (description, checksum) in REVIEWED_MIGRATIONS.items():
            item = required_by_version[version_number]
            if (
                item.get("description") != description
                or item.get("success") != 1
                or item.get("checksum_hex") != checksum
            ):
                raise SafetyError(f"Profile {profile_id} changes migration {version_number}")

        objects = profile.get("objects")
        if not isinstance(objects, list):
            raise SafetyError(f"Missing objects in {profile_id}")
        names = {item.get("name") for item in objects if isinstance(item, dict)}
        if names != KNOWN_OBJECTS or len(objects) != len(KNOWN_OBJECTS):
            raise SafetyError(
                f"Profile {profile_id} must contain exactly the reviewed compatibility objects"
            )
        for item in objects:
            name = item.get("name")
            object_type = item.get("type")
            ddl = item.get("ddl")
            expected_hash = item.get("normalized_sha256")
            if object_type not in {"table", "index"} or not isinstance(ddl, str):
                raise SafetyError(f"Invalid object definition for {name}")
            expected_type, reviewed_hash = REVIEWED_OBJECT_SPECS[name]
            if object_type != expected_type:
                raise SafetyError(f"Profile object {name} must remain a {expected_type}")
            if ";" in ddl or "--" in ddl or "/*" in ddl:
                raise SafetyError(f"Unsafe SQL in profile object {name}")
            prefix = f"create {object_type} {name}".casefold()
            if not normalize_sql(ddl).startswith(prefix):
                raise SafetyError(f"DDL does not create the declared object {name}")
            if sql_hash(ddl) != expected_hash:
                raise SafetyError(f"DDL hash mismatch for profile object {name}")
            if expected_hash != reviewed_hash:
                raise SafetyError(f"Profile object {name} differs from the reviewed DDL")
        if profile.get("install_order") != [
            "agent_jobs",
            "agent_job_items",
            "idx_agent_jobs_status",
            "idx_agent_job_items_status",
        ]:
            raise SafetyError(f"Unexpected install order in {profile_id}")
        if profile.get("remove_order") != [
            "idx_agent_job_items_status",
            "idx_agent_jobs_status",
            "agent_job_items",
            "agent_jobs",
        ]:
            raise SafetyError(f"Unexpected removal order in {profile_id}")
        expected_failure = {
            "official_cli_result": "failed to delete session",
            "error_substring": "no such table: agent_jobs",
            "partial_deletion": {
                "rollout_file_absent": True,
                "state_thread_row_present": True,
                "spawn_edge_present": True,
                "state_quick_check": "ok",
            },
            "safety_state": {
                "bulk_delete_started": False,
                "other_authorized_roots_deleted": 0,
                "database_backups_created": True,
                "automatic_compatibility_workaround_applied": False,
            },
        }
        if profile.get("failure_fingerprint") != expected_failure:
            raise SafetyError(f"Profile {profile_id} changes the reviewed failure fingerprint")
    return profiles


def load_profiles(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    resolved = canonical(path)
    document = load_json(resolved)
    return document, validate_profile_document(document), resolved


def profile_objects(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in profile["objects"]}


def inspect_database(
    database: Path,
    profile: dict[str, Any],
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    owns_connection = connection is None
    if connection is None:
        connection = readonly_connection(database)
    try:
        quick_row = connection.execute("PRAGMA quick_check").fetchone()
        quick = str(quick_row[0]) if quick_row else "missing-result"
        migration_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations'"
        ).fetchone()
        all_migrations: list[dict[str, Any]] = []
        if migration_table:
            rows = connection.execute(
                "SELECT version, description, success, hex(checksum) "
                "FROM _sqlx_migrations ORDER BY version"
            ).fetchall()
            all_migrations = [
                {
                    "version": int(row[0]),
                    "description": str(row[1]),
                    "success": int(row[2]),
                    "checksum_hex": str(row[3]).upper(),
                }
                for row in rows
            ]
        names = [item["name"] for item in profile["objects"]]
        placeholders = ",".join("?" for _ in names)
        rows = connection.execute(
            f"SELECT name, type, sql, rootpage FROM sqlite_master WHERE name IN ({placeholders})",
            names,
        ).fetchall()
        objects: dict[str, dict[str, Any]] = {}
        for name, object_type, sql, rootpage in rows:
            entry: dict[str, Any] = {
                "type": str(object_type),
                "normalized_sha256": sql_hash(str(sql)) if sql else None,
                "rootpage": int(rootpage),
            }
            if object_type == "table":
                safe_name = str(name)
                if safe_name not in KNOWN_OBJECTS:
                    raise SafetyError(f"Refusing unexpected table name: {safe_name}")
                entry["row_count"] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0]
                )
            objects[str(name)] = entry
        required_versions = {
            item["version"] for item in profile["migration"]["required"]
        }
        migrations = [
            item for item in all_migrations if item["version"] in required_versions
        ]
        max_successful = max(
            (item["version"] for item in all_migrations if item["success"] == 1),
            default=None,
        )
        failed = [item for item in all_migrations if item["success"] != 1]
        return {
            "quick_check": quick,
            "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            "migration_table_present": bool(migration_table),
            "migration_count": len(all_migrations),
            "max_successful_migration": max_successful,
            "failed_migrations": failed,
            "migrations": migrations,
            "objects": objects,
        }
    finally:
        if owns_connection:
            connection.close()


def migration_matches(inspection: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    rule = profile["migration"]
    if not inspection["migration_table_present"]:
        problems.append("_sqlx_migrations is missing")
        return problems
    if inspection["quick_check"] != "ok":
        problems.append(f"state quick_check is {inspection['quick_check']!r}")
    if inspection["failed_migrations"]:
        problems.append("failed migration rows are present")
    if inspection["max_successful_migration"] != rule["max_successful"]:
        problems.append(
            "maximum successful migration is "
            f"{inspection['max_successful_migration']!r}, expected {rule['max_successful']}"
        )
    rows = {item["version"]: item for item in inspection["migrations"]}
    for expected in rule["required"]:
        actual = rows.get(expected["version"])
        if actual is None:
            problems.append(f"required migration {expected['version']} is missing")
            continue
        for key in ("description", "success", "checksum_hex"):
            if actual[key] != expected[key]:
                problems.append(
                    f"migration {expected['version']} {key} differs from the verified profile"
                )
    return problems


def object_state(inspection: dict[str, Any], profile: dict[str, Any]) -> tuple[str, list[str]]:
    actual = inspection["objects"]
    expected = profile_objects(profile)
    present = set(actual)
    if not present:
        return "absent", []
    if present != KNOWN_OBJECTS:
        return "partial", [
            "compatibility objects are only partially present: " + ", ".join(sorted(present))
        ]
    problems: list[str] = []
    for name, rule in expected.items():
        item = actual[name]
        if item["type"] != rule["type"]:
            problems.append(f"{name} has type {item['type']}, expected {rule['type']}")
        if item["normalized_sha256"] != rule["normalized_sha256"]:
            problems.append(f"{name} schema fingerprint differs")
        if rule["type"] == "table" and item.get("row_count") != 0:
            problems.append(f"{name} is not empty")
    return ("exact" if not problems else "conflict"), problems


def read_codex_version(codex_exe: str | None) -> dict[str, Any]:
    executable = codex_exe or shutil.which("codex")
    if not executable:
        return {"ok": False, "version": None, "raw": None, "error": "codex executable not found"}
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "version": None, "raw": None, "error": str(exc)}
    raw = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", raw)
    if result.returncode != 0 or not match:
        return {
            "ok": False,
            "version": None,
            "raw": raw,
            "error": f"codex --version exited {result.returncode} or was unparseable",
        }
    return {"ok": True, "version": match.group(1), "raw": raw, "error": None}


def require_live_codex_version(codex_exe: str | None, expected_version: str) -> None:
    default_executable = shutil.which("codex")
    if not default_executable:
        raise SafetyError("Live compatibility writes require codex from PATH")
    if codex_exe:
        supplied = shutil.which(codex_exe) or codex_exe
        if canonical(Path(supplied)) != canonical(Path(default_executable)):
            raise SafetyError("Live compatibility writes do not accept an alternate codex executable")
    live = read_codex_version(None)
    if not live["ok"] or live["version"] != expected_version:
        raise SafetyError("Codex CLI version changed after compatibility diagnosis")


def compare_mapping(actual: Any, expected: dict[str, Any], prefix: str) -> list[str]:
    if not isinstance(actual, dict):
        return [f"{prefix} is missing or is not an object"]
    problems: list[str] = []
    for key, value in expected.items():
        if actual.get(key) != value:
            problems.append(f"{prefix}.{key} differs from the verified fingerprint")
    return problems


def validate_failure_evidence(
    path: Path,
    profile: dict[str, Any],
    codex_home: Path,
    cli_version: str,
    max_age_hours: int,
    now: dt.datetime,
) -> tuple[dict[str, Any], list[str], Path]:
    resolved = ensure_external_path(path, codex_home, must_exist=True)
    data = load_json(resolved)
    rule = profile["failure_fingerprint"]
    problems: list[str] = []
    age_hours = (now.timestamp() - resolved.stat().st_mtime) / 3600
    if age_hours < -1:
        problems.append("failure evidence file timestamp is in the future")
    elif age_hours > max_age_hours:
        problems.append(
            f"failure evidence is stale ({age_hours:.1f} hours old; maximum is {max_age_hours})"
        )
    if data.get("schema_version") != 1:
        problems.append("failure evidence schema_version is not 1")
    if data.get("codex_cli_version") != cli_version:
        problems.append("failure evidence CLI version differs from the live CLI")
    if data.get("official_cli_result") != rule["official_cli_result"]:
        problems.append("official CLI result differs from the verified fingerprint")
    message = data.get("app_server_error", {}).get("message")
    if not isinstance(message, str) or rule["error_substring"] not in message:
        problems.append("exact agent_jobs error substring is absent")
    else:
        missing_tables = re.findall(r"no such table:\s*([A-Za-z0-9_]+)", message)
        if missing_tables != ["agent_jobs"]:
            problems.append("missing-table error is not exactly agent_jobs")
    problems.extend(
        compare_mapping(data.get("partial_deletion"), rule["partial_deletion"], "partial_deletion")
    )
    problems.extend(
        compare_mapping(data.get("safety_state"), rule["safety_state"], "safety_state")
    )
    schema_evidence = data.get("schema_evidence")
    if not isinstance(schema_evidence, dict):
        problems.append("schema_evidence is missing")
    else:
        live_path = schema_evidence.get("live_state_database")
        if not isinstance(live_path, str):
            problems.append("schema_evidence.live_state_database is missing")
        else:
            try:
                if canonical(Path(live_path)) != canonical(codex_home / "state_5.sqlite"):
                    problems.append("failure evidence points to a different live state database")
            except SafetyError as exc:
                problems.append(str(exc))
        if schema_evidence.get("live_agent_jobs_present") is not False:
            problems.append("failure evidence did not record agent_jobs as absent")
    thread_id = data.get("canary_thread_id")
    if not isinstance(thread_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", thread_id
    ):
        problems.append("canary_thread_id is missing or invalid")
    return data, problems, resolved


def inspect_live_canary(
    codex_home: Path,
    state_db: Path,
    thread_id: str,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    owns_connection = connection is None
    if connection is None:
        connection = readonly_connection(state_db)
    try:
        thread_row_count = int(
            connection.execute("SELECT COUNT(*) FROM threads WHERE id = ?", (thread_id,)).fetchone()[0]
        )
        edge_rows = connection.execute(
            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges "
            "WHERE parent_thread_id = ? OR child_thread_id = ? "
            "ORDER BY parent_thread_id, child_thread_id",
            (thread_id, thread_id),
        ).fetchall()
        spawn_edges = [[str(row[0]), str(row[1])] for row in edge_rows]
    finally:
        if owns_connection:
            connection.close()
    rollout_matches: list[str] = []
    needle = thread_id.casefold()
    for folder_name in ("sessions", "archived_sessions"):
        root = codex_home / folder_name
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if not (Path(current) / name).is_symlink()
            ]
            for name in files:
                if needle in name.casefold() and name.casefold().endswith(".jsonl"):
                    rollout_matches.append(str(Path(current) / name))
    return {
        "thread_id": thread_id,
        "thread_row_count": thread_row_count,
        "spawn_edge_count": len(spawn_edges),
        "spawn_edges": spawn_edges,
        "spawn_edge_set_sha256": sha256_bytes(
            json.dumps(spawn_edges, separators=(",", ":")).encode("utf-8")
        ),
        "rollout_matches": rollout_matches,
        "matches_expected_partial_state": (
            thread_row_count == 1 and len(spawn_edges) >= 1 and not rollout_matches
        ),
    }


def validate_backup_summary(
    path: Path,
    document: dict[str, Any],
    codex_home: Path,
    now: dt.datetime,
) -> tuple[dict[str, Any], list[str], Path]:
    resolved = ensure_external_path(path, codex_home, must_exist=True)
    data = load_json(resolved)
    problems: list[str] = []
    if data.get("schema_version") != 1:
        problems.append("backup summary schema_version is not 1")
    try:
        summary_home = canonical(Path(str(data.get("codex_home"))))
        if summary_home != codex_home:
            problems.append("backup summary belongs to a different CodexHome")
    except SafetyError as exc:
        problems.append(str(exc))

    created_at: dt.datetime | None = None
    try:
        created_at = parse_datetime(data.get("created_at"), "backup created_at")
        age_minutes = (now - created_at).total_seconds() / 60
        max_age = int(document["policy"]["backup_max_age_minutes"])
        if age_minutes < -5:
            problems.append("backup timestamp is in the future")
        elif age_minutes > max_age:
            problems.append(
                f"backup is stale ({age_minutes:.1f} minutes old; maximum is {max_age})"
            )
    except (SafetyError, KeyError, TypeError, ValueError) as exc:
        problems.append(str(exc))

    backup_dir: Path | None = None
    try:
        backup_dir = ensure_external_path(
            Path(str(data.get("backup_dir"))), codex_home, must_exist=True
        )
        if not backup_dir.is_dir():
            problems.append("backup_dir is not a directory")
    except SafetyError as exc:
        problems.append(str(exc))

    rows = data.get("databases")
    if not isinstance(rows, list):
        problems.append("backup summary databases is missing")
        rows = []
    by_name = {
        item.get("database"): item
        for item in rows
        if isinstance(item, dict) and isinstance(item.get("database"), str)
    }
    if not REQUIRED_BACKUPS.issubset(by_name):
        missing = sorted(REQUIRED_BACKUPS - set(by_name))
        problems.append("required database backups are missing: " + ", ".join(missing))

    for name in sorted(REQUIRED_BACKUPS & set(by_name)):
        row = by_name[name]
        expected_source = canonical(codex_home / name)
        try:
            source = canonical(Path(str(row.get("source"))))
            if source != expected_source:
                problems.append(f"{name} source path differs")
            if row.get("source_bytes") != source.stat().st_size:
                problems.append(f"{name} live source size changed after backup")
        except (OSError, SafetyError) as exc:
            problems.append(str(exc))
        if row.get("source_quick_check") != "ok":
            problems.append(f"{name} source quick_check was not ok")
        if row.get("backup_quick_check") != "ok":
            problems.append(f"{name} recorded backup quick_check was not ok")
        try:
            backup = ensure_external_path(
                Path(str(row.get("backup"))), codex_home, must_exist=True
            )
            if not backup.is_file() or backup.is_symlink():
                problems.append(f"{name} backup is not a regular file")
                continue
            if backup_dir is not None and not is_below(backup, backup_dir):
                problems.append(f"{name} backup is outside backup_dir")
            actual_size = backup.stat().st_size
            if row.get("backup_bytes") != actual_size:
                problems.append(f"{name} backup size differs")
            expected_hash = row.get("backup_sha256")
            if not isinstance(expected_hash, str) or sha256_file(backup) != expected_hash.lower():
                problems.append(f"{name} backup SHA-256 differs")
            if quick_check_path(backup) != "ok":
                problems.append(f"{name} live backup quick_check failed")
        except (OSError, sqlite3.Error, SafetyError) as exc:
            problems.append(f"{name} backup validation failed: {exc}")
    return data, problems, resolved


def select_profile(
    profiles: Iterable[dict[str, Any]], cli_version: str
) -> dict[str, Any] | None:
    matches = [profile for profile in profiles if cli_version in profile["codex_cli_versions"]]
    if len(matches) > 1:
        raise SafetyError(f"Multiple compatibility profiles match CLI {cli_version}")
    return matches[0] if matches else None


def diagnose(
    codex_home: Path,
    profile_path: Path,
    codex_exe: str | None,
    failure_evidence: Path | None,
    backup_summary: Path | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    codex_home = canonical(codex_home)
    state_db = require_live_state_database(codex_home, "state_5.sqlite")
    document, profiles, resolved_profile = load_profiles(profile_path)
    version = read_codex_version(codex_exe)
    report: dict[str, Any] = {
        "schema_version": 1,
        "operation": "diagnose",
        "checked_at": now.isoformat(),
        "codex_home": str(codex_home),
        "state_database": str(state_db),
        "profile_file": str(resolved_profile),
        "profile_sha256": sha256_file(resolved_profile),
        "cli": version,
        "matched_profile_id": None,
        "database": None,
        "failure_evidence": None,
        "backup_summary": None,
        "decision": "unsafe_stop",
        "reasons": [],
        "next_action": "Stop deletion and inspect the diagnostic evidence.",
    }
    if not version["ok"]:
        report["reasons"].append(version["error"])
        return report

    profile = select_profile(profiles, version["version"])
    if profile is None:
        report["decision"] = "unsupported_update_required"
        report["reasons"].append(
            f"CLI {version['version']} has no reviewed CLI/schema compatibility profile"
        )
        report["next_action"] = (
            "Do not delete. Check for a trusted skill update; otherwise collect evidence and "
            "prepare a tested Draft PR."
        )
        return report
    report["matched_profile_id"] = profile["id"]
    inspection = inspect_database(state_db, profile)
    report["database"] = inspection

    review_after = min(
        parse_date(document["review_after"], "document review_after"),
        parse_date(profile["review_after"], "profile review_after"),
    )
    if now.date() > review_after:
        report["decision"] = "stale_profile_update_required"
        report["reasons"].append(
            f"profile review deadline {review_after.isoformat()} has passed"
        )
        report["next_action"] = (
            "Do not install the workaround. Refresh or revalidate the skill and profile first."
        )
        return report

    migration_problems = migration_matches(inspection, profile)
    if migration_problems:
        report["decision"] = "unsupported_update_required"
        report["reasons"].extend(migration_problems)
        report["next_action"] = (
            "This is not the reviewed database schema. Stop deletion and update the skill."
        )
        return report

    state, object_problems = object_state(inspection, profile)
    if state == "exact":
        report["decision"] = "compat_installed"
        report["reasons"].append("all temporary compatibility objects match and are empty")
        report["next_action"] = (
            "Do not reinstall. Continue only under the original authorized incident, then remove "
            "the objects with the matching install result."
        )
        return report
    if state != "absent":
        report["reasons"].extend(object_problems)
        report["next_action"] = (
            "Stop deletion. Do not repair, replace, or drop partially matching objects automatically."
        )
        return report

    if failure_evidence is None:
        report["decision"] = "canary_required"
        report["reasons"].append(
            "the known CLI/schema conflict is present, but no exact canary failure was supplied"
        )
        report["next_action"] = (
            "Create fresh online database backups and run exactly one official delete canary. "
            "Do not install compatibility objects pre-emptively."
        )
        return report

    failure_data, failure_problems, failure_path = validate_failure_evidence(
        failure_evidence,
        profile,
        codex_home,
        version["version"],
        int(document["policy"]["failure_evidence_max_age_hours"]),
        now,
    )
    live_canary: dict[str, Any] | None = None
    if not failure_problems:
        try:
            live_canary = inspect_live_canary(
                codex_home, state_db, failure_data["canary_thread_id"]
            )
            if not live_canary["matches_expected_partial_state"]:
                failure_problems.append(
                    "live canary row, spawn edge, or rollout state differs from the recorded partial deletion"
                )
        except (KeyError, sqlite3.Error, OSError) as exc:
            failure_problems.append(f"live canary state could not be verified: {exc}")
    report["failure_evidence"] = {
        "path": str(failure_path),
        "sha256": sha256_file(failure_path),
        "valid": not failure_problems,
        "problems": failure_problems,
        "live_canary": live_canary,
    }
    if failure_problems:
        report["reasons"].extend(failure_problems)
        report["next_action"] = (
            "Stop deletion. This canary failure is not the exact reviewed incident fingerprint."
        )
        return report
    if backup_summary is None:
        report["reasons"].append("a fresh external backup summary is required")
        report["next_action"] = (
            "Create new online backups of state, goals, and memories outside CodexHome."
        )
        return report

    _, backup_problems, backup_path = validate_backup_summary(
        backup_summary, document, codex_home, now
    )
    report["backup_summary"] = {
        "path": str(backup_path),
        "sha256": sha256_file(backup_path),
        "valid": not backup_problems,
        "problems": backup_problems,
    }
    if backup_problems:
        report["reasons"].extend(backup_problems)
        report["next_action"] = (
            "Stop deletion and create a new validated online backup set immediately before retry."
        )
        return report

    report["decision"] = "known_workaround_eligible"
    report["reasons"].append(
        "CLI, migration checksums, missing objects, exact canary failure, and fresh backups match"
    )
    report["next_action"] = (
        "A human may explicitly authorize the temporary compatibility install. After installation, "
        "retry only the same canary through the official delete path."
    )
    return report


def prepare_output_path(path: Path | None, codex_home: Path) -> Path:
    if path is None:
        raise SafetyError("Execution requires --output outside CodexHome")
    parent = ensure_external_path(path.parent, codex_home, must_exist=True)
    resolved = parent / path.name
    if resolved.exists():
        raise SafetyError(f"Refusing to overwrite audit output: {resolved}")
    return resolved


def write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def emit(value: dict[str, Any], output: Path | None = None) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    if output is not None:
        write_json_exclusive(output, value)
    print(payload)


def require_decision(report: dict[str, Any], expected: str) -> None:
    if report.get("decision") != expected:
        reasons = "; ".join(report.get("reasons", [])) or "no reason supplied"
        raise SafetyError(
            f"Required decision {expected!r}, got {report.get('decision')!r}: {reasons}"
        )


def install_compat(args: argparse.Namespace) -> dict[str, Any]:
    report = diagnose(
        Path(args.codex_home),
        Path(args.profile_file),
        args.codex_exe,
        Path(args.failure_evidence),
        Path(args.backup_summary),
    )
    require_decision(report, "known_workaround_eligible")
    report["operation"] = "install-plan"
    report["execute_requested"] = bool(args.execute)
    report["confirmation_token_required"] = INSTALL_TOKEN
    if not args.execute:
        report["next_action"] = (
            f"Review this plan. Execution additionally requires --execute --confirm-token {INSTALL_TOKEN} "
            "and a new --output path outside CodexHome."
        )
        return report
    if args.confirm_token != INSTALL_TOKEN:
        raise SafetyError("Invalid install confirmation token")

    codex_home = canonical(Path(args.codex_home))
    output = prepare_output_path(Path(args.output) if args.output else None, codex_home)
    require_repository_profile(Path(args.profile_file))
    _, profiles, profile_path = load_profiles(Path(args.profile_file))
    if sha256_file(profile_path) != report["profile_sha256"]:
        raise SafetyError("Compatibility profile changed after diagnosis")
    profile = next(item for item in profiles if item["id"] == report["matched_profile_id"])
    state_db = require_live_state_database(codex_home, profile["state_database"])
    expected_objects = profile_objects(profile)
    require_live_codex_version(args.codex_exe, report["cli"]["version"])
    if sha256_file(Path(report["failure_evidence"]["path"])) != report["failure_evidence"]["sha256"]:
        raise SafetyError("Canary failure evidence changed after diagnosis")
    if sha256_file(Path(report["backup_summary"]["path"])) != report["backup_summary"]["sha256"]:
        raise SafetyError("Backup summary changed after diagnosis")

    document = load_json(profile_path)
    _, backup_problems, _ = validate_backup_summary(
        Path(report["backup_summary"]["path"]), document, codex_home, utc_now()
    )
    if backup_problems:
        raise SafetyError("Backup revalidation failed: " + "; ".join(backup_problems))

    journal = {
        "schema_version": 1,
        "operation": "install",
        "status": "prepared",
        "prepared_at": iso_now(),
        "codex_home": str(codex_home),
        "state_database": str(state_db),
        "profile_file": str(profile_path),
        "profile_sha256": sha256_file(profile_path),
        "profile_id": profile["id"],
        "run_nonce": secrets.token_hex(16),
        "database_identity": database_identity(state_db),
        "codex_cli_version": report["cli"]["version"],
        "migration_max_successful": profile["migration"]["max_successful"],
        "created_objects": {
            name: expected_objects[name]["normalized_sha256"]
            for name in profile["install_order"]
        },
        "failure_evidence": report["failure_evidence"]["path"],
        "failure_evidence_sha256": report["failure_evidence"]["sha256"],
        "canary_thread_id": report["failure_evidence"]["live_canary"]["thread_id"],
        "backup_summary": report["backup_summary"]["path"],
        "backup_summary_sha256": report["backup_summary"]["sha256"],
        "quick_check": "pending",
        "output": str(output),
    }
    write_json_exclusive(output, journal)

    connection = sqlite3.connect(str(state_db), timeout=2.0)
    committed = False
    commit_attempted = False
    try:
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        inspection = inspect_database(state_db, profile, connection)
        migration_problems = migration_matches(inspection, profile)
        state, object_problems = object_state(inspection, profile)
        if migration_problems or state != "absent":
            raise SafetyError(
                "Live state changed before install: "
                + "; ".join(migration_problems + object_problems + [f"object_state={state}"])
            )
        live_canary = inspect_live_canary(
            codex_home,
            state_db,
            load_json(Path(report["failure_evidence"]["path"]))["canary_thread_id"],
            connection,
        )
        recorded_canary = report["failure_evidence"]["live_canary"]
        if (
            not live_canary["matches_expected_partial_state"]
            or live_canary["thread_row_count"] != recorded_canary["thread_row_count"]
            or live_canary["spawn_edge_set_sha256"]
            != recorded_canary["spawn_edge_set_sha256"]
            or live_canary["rollout_matches"] != recorded_canary["rollout_matches"]
        ):
            raise SafetyError("Live canary partial-deletion state changed before install")
        for name in profile["install_order"]:
            connection.execute(expected_objects[name]["ddl"])
        after = inspect_database(state_db, profile, connection)
        after_state, after_problems = object_state(after, profile)
        if after_state != "exact" or after_problems:
            raise SafetyError("Installed compatibility objects failed verification")
        if after["max_successful_migration"] != inspection["max_successful_migration"]:
            raise SafetyError("Migration history changed during compatibility install")
        if after["quick_check"] != "ok":
            raise SafetyError("State database quick_check failed after compatibility install")
        journal["status"] = "commit-pending"
        journal["installed_schema_version"] = after["schema_version"]
        journal["created_object_rootpages"] = {
            name: after["objects"][name]["rootpage"] for name in profile["install_order"]
        }
        journal["recorded_at"] = iso_now()
        write_json_atomic(output, journal)
        commit_attempted = True
        connection.commit()
        committed = True
    except Exception as exc:
        rollback_ok = False
        if not committed:
            try:
                connection.rollback()
                rollback_ok = True
            except sqlite3.Error:
                rollback_ok = False
        journal["status"] = (
            "commit-outcome-unknown"
            if commit_attempted
            else ("rolled-back" if rollback_ok else "rollback-outcome-unknown")
        )
        journal["error"] = str(exc)
        journal["recorded_at"] = iso_now()
        try:
            write_json_atomic(output, journal)
        except OSError:
            pass
        raise
    finally:
        connection.close()

    post_commit_check = quick_check_path(state_db)
    if post_commit_check != "ok":
        journal["status"] = "installed-needs-attention"
        journal["quick_check"] = post_commit_check
        journal["recorded_at"] = iso_now()
        write_json_atomic(output, journal)
        raise SafetyError(
            "Compatibility install committed, but post-commit quick_check failed; stop immediately"
        )
    result = dict(journal)
    result.update(
        {
            "status": "installed",
            "created_at": iso_now(),
            "quick_check": "ok",
            "next_action": (
                "Retry only the same canary through official codex delete. Stop on any failure or "
                "unexpected state. Do not refresh the skill mid-incident."
            ),
        }
    )
    write_json_atomic(output, result)
    return result


DROP_STATEMENTS = {
    "idx_agent_job_items_status": "DROP INDEX idx_agent_job_items_status",
    "idx_agent_jobs_status": "DROP INDEX idx_agent_jobs_status",
    "agent_job_items": "DROP TABLE agent_job_items",
    "agent_jobs": "DROP TABLE agent_jobs",
}


def remove_compat(args: argparse.Namespace) -> dict[str, Any]:
    codex_home = canonical(Path(args.codex_home))
    if args.execute:
        require_repository_profile(Path(args.profile_file))
    document, profiles, profile_path = load_profiles(Path(args.profile_file))
    install_path = ensure_external_path(Path(args.install_result), codex_home, must_exist=True)
    install = load_json(install_path)
    profile_id = install.get("profile_id")
    profile = next((item for item in profiles if item["id"] == profile_id), None)
    if profile is None:
        raise SafetyError("Install result references an unknown profile")
    expected_objects = profile_objects(profile)
    state_db = require_live_state_database(codex_home, profile["state_database"])
    problems: list[str] = []
    if install.get("operation") != "install" or install.get("status") != "installed":
        problems.append("install result does not record a completed install")
    if install.get("profile_sha256") != sha256_file(profile_path):
        problems.append("profile changed after installation")
    if not isinstance(install.get("run_nonce"), str) or not re.fullmatch(
        r"[0-9a-f]{32}", install["run_nonce"]
    ):
        problems.append("install result run nonce is missing or invalid")
    if install.get("state_database") != str(state_db):
        problems.append("install result references a different state database")
    if install.get("database_identity") != database_identity(state_db):
        problems.append("live database file identity differs from installation")
    if install.get("migration_max_successful") != profile["migration"]["max_successful"]:
        problems.append("install result migration does not match")
    expected_hashes = {
        name: expected_objects[name]["normalized_sha256"] for name in profile["install_order"]
    }
    if install.get("created_objects") != expected_hashes:
        problems.append("install result object fingerprints differ")
    expected_rootpages = install.get("created_object_rootpages")
    if (
        not isinstance(expected_rootpages, dict)
        or set(expected_rootpages) != set(profile["install_order"])
        or not all(isinstance(value, int) and value > 0 for value in expected_rootpages.values())
    ):
        problems.append("install result object rootpages are missing or invalid")
    if not isinstance(install.get("installed_schema_version"), int):
        problems.append("install result schema_version is missing")
    try:
        require_live_codex_version(None, str(install.get("codex_cli_version")))
    except SafetyError as exc:
        problems.append(str(exc))
    try:
        installed_at = parse_datetime(install.get("created_at"), "install created_at")
        age_hours = (utc_now() - installed_at).total_seconds() / 3600
        maximum_age = int(document["policy"]["compat_install_max_age_hours"])
        if age_hours < -1 or age_hours > maximum_age:
            problems.append(
                f"install result is outside the {maximum_age}-hour automatic removal window"
            )
        file_time = dt.datetime.fromtimestamp(install_path.stat().st_mtime, dt.timezone.utc)
        if abs((file_time - installed_at).total_seconds()) > 300:
            problems.append("install result file timestamp differs from its recorded install time")
    except (KeyError, OSError, SafetyError, TypeError, ValueError) as exc:
        problems.append(str(exc))
    for path_key, hash_key in (
        ("failure_evidence", "failure_evidence_sha256"),
        ("backup_summary", "backup_summary_sha256"),
    ):
        try:
            evidence_path = ensure_external_path(
                Path(str(install.get(path_key))), codex_home, must_exist=True
            )
            if sha256_file(evidence_path) != install.get(hash_key):
                problems.append(f"{path_key} changed after compatibility installation")
        except (OSError, SafetyError) as exc:
            problems.append(str(exc))
    if problems:
        raise SafetyError("; ".join(problems))

    inspection = inspect_database(state_db, profile)
    migration_problems = migration_matches(inspection, profile)
    state, object_problems = object_state(inspection, profile)
    if inspection["schema_version"] != install.get("installed_schema_version"):
        object_problems.append("live SQLite schema_version changed after installation")
    if isinstance(expected_rootpages, dict):
        actual_rootpages = {
            name: inspection["objects"].get(name, {}).get("rootpage")
            for name in profile["install_order"]
        }
        if actual_rootpages != expected_rootpages:
            object_problems.append("compatibility object rootpages changed after installation")
    plan = {
        "schema_version": 1,
        "operation": "remove-plan",
        "checked_at": iso_now(),
        "codex_home": str(codex_home),
        "state_database": str(state_db),
        "profile_id": profile["id"],
        "install_result": str(install_path),
        "object_state": state,
        "problems": migration_problems + object_problems,
        "remove_order": profile["remove_order"],
        "execute_requested": bool(args.execute),
        "confirmation_token_required": REMOVE_TOKEN,
    }
    if migration_problems or state != "exact" or object_problems:
        raise SafetyError(
            "Compatibility objects cannot be safely removed: "
            + "; ".join(migration_problems + object_problems + [f"object_state={state}"])
        )
    if not args.execute:
        plan["next_action"] = (
            f"Review this plan. Execution additionally requires --execute --confirm-token {REMOVE_TOKEN} "
            "and a new --output path outside CodexHome."
        )
        return plan
    if args.confirm_token != REMOVE_TOKEN:
        raise SafetyError("Invalid removal confirmation token")
    output = prepare_output_path(Path(args.output) if args.output else None, codex_home)

    journal = {
        "schema_version": 1,
        "operation": "remove",
        "status": "prepared",
        "prepared_at": iso_now(),
        "codex_home": str(codex_home),
        "state_database": str(state_db),
        "profile_id": profile["id"],
        "install_result": str(install_path),
        "install_result_sha256": sha256_file(install_path),
        "removed_objects": profile["remove_order"],
        "migration_max_successful": profile["migration"]["max_successful"],
        "quick_check": "pending",
        "output": str(output),
    }
    write_json_exclusive(output, journal)

    connection = sqlite3.connect(str(state_db), timeout=2.0)
    committed = False
    commit_attempted = False
    try:
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        live = inspect_database(state_db, profile, connection)
        live_migration_problems = migration_matches(live, profile)
        live_state, live_object_problems = object_state(live, profile)
        if live_migration_problems or live_state != "exact" or live_object_problems:
            raise SafetyError("Live state changed before compatibility removal")
        if live["schema_version"] != install["installed_schema_version"]:
            raise SafetyError("SQLite schema_version changed before compatibility removal")
        live_rootpages = {
            name: live["objects"][name]["rootpage"] for name in profile["install_order"]
        }
        if live_rootpages != expected_rootpages:
            raise SafetyError("Compatibility object rootpages changed before removal")
        for name in profile["remove_order"]:
            connection.execute(DROP_STATEMENTS[name])
        after = inspect_database(state_db, profile, connection)
        after_state, after_problems = object_state(after, profile)
        if after_state != "absent" or after_problems:
            raise SafetyError("Compatibility objects remain after removal")
        if after["max_successful_migration"] != profile["migration"]["max_successful"]:
            raise SafetyError("Migration history changed during compatibility removal")
        if after["quick_check"] != "ok":
            raise SafetyError("State database quick_check failed after compatibility removal")
        commit_attempted = True
        connection.commit()
        committed = True
    except Exception as exc:
        rollback_ok = False
        if not committed:
            try:
                connection.rollback()
                rollback_ok = True
            except sqlite3.Error:
                rollback_ok = False
        journal["status"] = (
            "commit-outcome-unknown"
            if commit_attempted
            else ("rolled-back" if rollback_ok else "rollback-outcome-unknown")
        )
        journal["error"] = str(exc)
        journal["recorded_at"] = iso_now()
        try:
            write_json_atomic(output, journal)
        except OSError:
            pass
        raise
    finally:
        connection.close()

    post_commit_check = quick_check_path(state_db)
    if post_commit_check != "ok":
        journal["status"] = "removed-needs-attention"
        journal["quick_check"] = post_commit_check
        journal["recorded_at"] = iso_now()
        write_json_atomic(output, journal)
        raise SafetyError(
            "Compatibility removal committed, but post-commit quick_check failed; stop immediately"
        )
    result = dict(journal)
    result.update(
        {
            "status": "removed",
            "removed_at": iso_now(),
            "quick_check": "ok",
        }
    )
    write_json_atomic(output, result)
    return result


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--profile-file", default=str(DEFAULT_PROFILE))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose and manage the reviewed Codex subagent-delete compatibility shim."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    diagnose_parser = subparsers.add_parser("diagnose", help="Read-only compatibility diagnosis")
    add_common_arguments(diagnose_parser)
    diagnose_parser.add_argument("--codex-exe")
    diagnose_parser.add_argument("--failure-evidence")
    diagnose_parser.add_argument("--backup-summary")
    diagnose_parser.add_argument("--output")

    install_parser = subparsers.add_parser("install", help="Plan or install the exact reviewed shim")
    add_common_arguments(install_parser)
    install_parser.add_argument("--codex-exe")
    install_parser.add_argument("--failure-evidence", required=True)
    install_parser.add_argument("--backup-summary", required=True)
    install_parser.add_argument("--execute", action="store_true")
    install_parser.add_argument("--confirm-token")
    install_parser.add_argument("--output")

    remove_parser = subparsers.add_parser("remove", help="Plan or remove objects from this run")
    add_common_arguments(remove_parser)
    remove_parser.add_argument("--install-result", required=True)
    remove_parser.add_argument("--execute", action="store_true")
    remove_parser.add_argument("--confirm-token")
    remove_parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "diagnose":
            report = diagnose(
                Path(args.codex_home),
                Path(args.profile_file),
                args.codex_exe,
                Path(args.failure_evidence) if args.failure_evidence else None,
                Path(args.backup_summary) if args.backup_summary else None,
            )
            output: Path | None = None
            if args.output:
                output = prepare_output_path(Path(args.output), canonical(Path(args.codex_home)))
            emit(report, output)
            return DECISION_EXIT_CODES.get(report["decision"], 4)
        if args.command == "install":
            result = install_compat(args)
            emit(result)
            return 0
        if args.command == "remove":
            result = remove_compat(args)
            emit(result)
            return 0
        raise SafetyError(f"Unknown command: {args.command}")
    except (SafetyError, sqlite3.Error, OSError) as exc:
        error = {
            "schema_version": 1,
            "operation": getattr(args, "command", "unknown"),
            "status": "stopped",
            "error": str(exc),
            "database_modification_status": (
                "not_requested" if getattr(args, "command", None) == "diagnose" else "unknown; inspect live state"
            ),
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
