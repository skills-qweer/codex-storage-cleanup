#!/usr/bin/env python3
"""Diagnose and manage one narrowly verified Codex delete compatibility shim.

The default and ``diagnose`` paths are read-only. ``install`` and ``remove``
require exact profile matches, explicit execution flags, confirmation tokens,
and audit output outside CodexHome. This module never invokes a delete API.
"""

from __future__ import annotations

import argparse
import ctypes
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
DEFAULT_TAIL_CERTIFICATES = (
    REPO_ROOT / "references" / "subagent-delete-tail-certificates.json"
)
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
REVIEWED_PROFILE_ID = "codex-0.142.2-state-migration-42-tail44-missing-agent-jobs-v2"
REVIEWED_DELETE_CONTRACT_ID = "codex-0.142.2-delete-threads-strict-agent-jobs-v1"
REVIEWED_TAIL_CHAIN_ID = "codex-state-42-to-44-delete-unrelated-v1"
REVIEWED_NATIVE_MINIMUM = "0.145.0"
REVIEWED_UPDATED_AT = "2026-08-03"
REVIEWED_REVIEW_AFTER = "2026-11-01"
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
REVIEWED_TAIL_MIGRATIONS = {
    43: {
        "description": "threads is pinned",
        "success": 1,
        "checksum_hex": (
            "9B2A199A557F5A92B0E27574E8BFC01DCE5EF28F106A3C16DFB3B8A6CD5679F3"
            "B8BC26B63E09C461A9A0C1364F0B04BF"
        ),
        "source_commit": "400ee190c30d5e4a88549c070a2335311f0baa91",
        "source_url": (
            "https://github.com/openai/codex/blob/400ee190c30d5e4a88549c070a2335311f0baa91/"
            "codex-rs/state/migrations/0043_threads_is_pinned.sql"
        ),
        "reviewed_effects": [
            "add_column:threads.is_pinned",
            "create_index:idx_threads_pinned_recency_at_ms",
        ],
        "delete_review": "delete_threads_strict body unchanged",
    },
    44: {
        "description": "external agent config imports provider id",
        "success": 1,
        "checksum_hex": (
            "DFA22384943E691A089B9E8A6A8DA988EF1E25FF51DB7975CA42C3EC4BE474370F"
            "262CAA77AEB5877EEEABBFF2C3CEB1"
        ),
        "source_commit": "ce803c45aed425b08b94d8e3c5fb7db0d2193568",
        "source_url": (
            "https://github.com/openai/codex/blob/ce803c45aed425b08b94d8e3c5fb7db0d2193568/"
            "codex-rs/state/migrations/0044_external_agent_config_imports_provider_id.sql"
        ),
        "reviewed_effects": [
            "add_nullable_column:external_agent_config_imports.provider_id"
        ],
        "delete_review": "no state thread runtime or thread_delete change",
    },
}
CODEX_VERSION_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)(?![0-9A-Za-z])"
)
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


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return sha256_bytes(payload)


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


def validate_required_migrations(
    required: Any, expected: dict[int, tuple[str, str]], label: str
) -> None:
    if not isinstance(required, list) or not required:
        raise SafetyError(f"Missing required migrations in {label}")
    by_version: dict[int, dict[str, Any]] = {}
    for item in required:
        if not isinstance(item, dict) or set(item) != {
            "version",
            "description",
            "success",
            "checksum_hex",
        }:
            raise SafetyError(f"Invalid required migration in {label}")
        version = item.get("version")
        checksum = item.get("checksum_hex")
        if (
            not isinstance(version, int)
            or version in by_version
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9A-F]{96}", checksum)
        ):
            raise SafetyError(f"Invalid required migration in {label}")
        by_version[version] = item
    if set(by_version) != set(expected):
        raise SafetyError(f"{label} changes the reviewed migration set")
    for version, (description, checksum) in expected.items():
        if by_version[version] != {
            "version": version,
            "description": description,
            "success": 1,
            "checksum_hex": checksum,
        }:
            raise SafetyError(f"{label} changes migration {version}")


def validate_profile_document(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("schema_version") != 2:
        raise SafetyError("Unsupported compatibility profile schema_version")
    if set(document) != {
        "schema_version",
        "updated_at",
        "review_after",
        "policy",
        "native_delete",
        "profiles",
    }:
        raise SafetyError("Compatibility profile contains unknown or missing fields")
    parse_date(document.get("updated_at"), "profile updated_at")
    parse_date(document.get("review_after"), "profile review_after")
    if (
        document.get("updated_at") != REVIEWED_UPDATED_AT
        or document.get("review_after") != REVIEWED_REVIEW_AFTER
    ):
        raise SafetyError("Compatibility profile review dates differ from the reviewed release")
    policy = document.get("policy")
    if not isinstance(policy, dict):
        raise SafetyError("Compatibility document policy is missing")
    if set(policy) != {
        "version_alone_is_never_sufficient",
        "backup_max_age_minutes",
        "failure_evidence_max_age_hours",
        "compat_install_max_age_hours",
        "runtime_evidence_max_age_minutes",
        "unknown_combinations",
        "automatic_database_workaround",
        "automatic_repository_merge",
    }:
        raise SafetyError("Compatibility policy contains unknown or missing fields")
    for key in (
        "backup_max_age_minutes",
        "failure_evidence_max_age_hours",
        "compat_install_max_age_hours",
        "runtime_evidence_max_age_minutes",
    ):
        if not isinstance(policy.get(key), int) or int(policy[key]) <= 0:
            raise SafetyError(f"Compatibility policy {key} must be a positive integer")
    if (
        policy["backup_max_age_minutes"] != 60
        or policy["failure_evidence_max_age_hours"] != 24
        or policy["compat_install_max_age_hours"] != 24
        or policy["runtime_evidence_max_age_minutes"] != 15
        or policy.get("version_alone_is_never_sufficient") is not True
        or policy.get("automatic_database_workaround") is not False
        or policy.get("automatic_repository_merge") is not False
    ):
        raise SafetyError("Critical compatibility policy differs from the reviewed limits")

    native = document.get("native_delete")
    if not isinstance(native, dict):
        raise SafetyError("native_delete policy is missing")
    if set(native) != {
        "minimum_codex_cli_version",
        "requires_desktop_runtime_hash_match",
        "requires_valid_openai_signature",
        "required_migrations",
        "compatibility_objects",
        "official_canary_required",
        "workaround_allowed",
        "sources",
    }:
        raise SafetyError("native_delete contains unknown or missing fields")
    if (
        native.get("minimum_codex_cli_version") != REVIEWED_NATIVE_MINIMUM
        or native.get("requires_desktop_runtime_hash_match") is not True
        or native.get("requires_valid_openai_signature") is not True
        or native.get("compatibility_objects") != "must_be_absent"
        or native.get("official_canary_required") is not True
        or native.get("workaround_allowed") is not False
    ):
        raise SafetyError("Native delete controls differ from the reviewed policy")
    validate_required_migrations(
        native.get("required_migrations"), REVIEWED_MIGRATIONS, "native_delete"
    )
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != 1:
        raise SafetyError("This script supports exactly one reviewed compatibility profile")

    seen_ids: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise SafetyError("Each compatibility profile must be an object")
        if set(profile) != {
            "id",
            "delete_contract_id",
            "status",
            "validated_at",
            "review_after",
            "codex_cli_versions",
            "state_database",
            "migration",
            "failure_fingerprint",
            "objects",
            "install_order",
            "remove_order",
            "sources",
        }:
            raise SafetyError("Legacy profile contains unknown or missing fields")
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
        if profile.get("delete_contract_id") != REVIEWED_DELETE_CONTRACT_ID:
            raise SafetyError(f"Profile {profile_id} changes the reviewed delete contract")
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
        if (
            profile.get("validated_at") != REVIEWED_UPDATED_AT
            or profile.get("review_after") != REVIEWED_REVIEW_AFTER
        ):
            raise SafetyError(f"Profile {profile_id} review dates are not the reviewed values")

        migration = profile.get("migration")
        if not isinstance(migration, dict):
            raise SafetyError(f"Invalid migration rule in {profile_id}")
        if set(migration) != {"base_through", "required", "tail_chain_id"}:
            raise SafetyError(f"Migration rule in {profile_id} contains unknown fields")
        if migration.get("base_through") != 42:
            raise SafetyError(f"Profile {profile_id} changes the reviewed migration base")
        if migration.get("tail_chain_id") != REVIEWED_TAIL_CHAIN_ID:
            raise SafetyError(f"Profile {profile_id} changes the reviewed tail chain")
        validate_required_migrations(
            migration.get("required"), REVIEWED_MIGRATIONS, profile_id
        )

        objects = profile.get("objects")
        if not isinstance(objects, list):
            raise SafetyError(f"Missing objects in {profile_id}")
        names = {item.get("name") for item in objects if isinstance(item, dict)}
        if names != KNOWN_OBJECTS or len(objects) != len(KNOWN_OBJECTS):
            raise SafetyError(
                f"Profile {profile_id} must contain exactly the reviewed compatibility objects"
            )
        for item in objects:
            if set(item) != {"name", "type", "ddl", "normalized_sha256"}:
                raise SafetyError("Compatibility object contains unknown or missing fields")
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


def validate_tail_certificates(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != 1:
        raise SafetyError("Unsupported tail-certificate schema_version")
    parse_date(document.get("updated_at"), "tail-certificate updated_at")
    parse_date(document.get("review_after"), "tail-certificate review_after")
    if (
        document.get("updated_at") != REVIEWED_UPDATED_AT
        or document.get("review_after") != REVIEWED_REVIEW_AFTER
    ):
        raise SafetyError("Tail-certificate review dates differ from the reviewed release")
    chains = document.get("chains")
    if not isinstance(chains, list) or len(chains) != 1:
        raise SafetyError("Exactly one reviewed tail chain is required")
    chain = chains[0]
    if not isinstance(chain, dict) or set(chain) != {
        "id",
        "delete_contract_id",
        "base_through",
        "max_successful",
        "classification",
        "migrations",
    }:
        raise SafetyError("Tail chain contains unsupported fields or is incomplete")
    if (
        chain.get("id") != REVIEWED_TAIL_CHAIN_ID
        or chain.get("delete_contract_id") != REVIEWED_DELETE_CONTRACT_ID
        or chain.get("base_through") != 42
        or chain.get("max_successful") != 44
        or chain.get("classification") != "reviewed_delete_unrelated"
    ):
        raise SafetyError("Tail chain differs from the independently reviewed chain")
    migrations = chain.get("migrations")
    if not isinstance(migrations, list) or [item.get("version") for item in migrations] != [43, 44]:
        raise SafetyError("Tail migrations must be the exact contiguous 43-to-44 chain")
    for item in migrations:
        version = item.get("version")
        expected = REVIEWED_TAIL_MIGRATIONS.get(version)
        expected_keys = {"version", *expected.keys()} if expected else set()
        if not expected or set(item) != expected_keys or item != {"version": version, **expected}:
            raise SafetyError(f"Tail migration {version!r} differs from the reviewed certificate")
        commit = item["source_commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or f"/blob/{commit}/" not in item["source_url"]:
            raise SafetyError(f"Tail migration {version} does not use an immutable source URL")
    return chain


def load_profiles(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    resolved = canonical(path)
    document = load_json(resolved)
    return document, validate_profile_document(document), resolved


def load_tail_certificate() -> tuple[dict[str, Any], Path]:
    resolved = canonical(DEFAULT_TAIL_CERTIFICATES)
    document = load_json(resolved)
    return validate_tail_certificates(document), resolved


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
                "SELECT version, description, installed_on, success, hex(checksum), execution_time "
                "FROM _sqlx_migrations ORDER BY version"
            ).fetchall()
            all_migrations = [
                {
                    "version": int(row[0]),
                    "description": str(row[1]),
                    "installed_on": str(row[2]),
                    "success": int(row[3]),
                    "checksum_hex": str(row[4]).upper(),
                    "execution_time": int(row[5]),
                }
                for row in rows
            ]
        contract_migrations = [
            {
                key: item[key]
                for key in ("version", "description", "success", "checksum_hex")
            }
            for item in all_migrations
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
            item for item in contract_migrations if item["version"] in required_versions
        ]
        max_successful = max(
            (item["version"] for item in all_migrations if item["success"] == 1),
            default=None,
        )
        failed = [item for item in all_migrations if item["success"] != 1]
        base_placeholders = ",".join("?" for _ in KNOWN_OBJECTS)
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            f"AND name NOT IN ({base_placeholders}) "
            "ORDER BY type, name, tbl_name",
            sorted(KNOWN_OBJECTS),
        ).fetchall()
        base_schema = [
            {
                "type": str(row[0]),
                "name": str(row[1]),
                "table": str(row[2]),
                "sql": None if row[3] is None else str(row[3]),
            }
            for row in schema_rows
        ]
        return {
            "quick_check": quick,
            "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            "migration_table_present": bool(migration_table),
            "migration_count": len(all_migrations),
            "max_successful_migration": max_successful,
            "failed_migrations": failed,
            "migrations": migrations,
            "migration_history": all_migrations,
            "migration_history_sha256": canonical_json_sha256(all_migrations),
            "migration_contract_sha256": canonical_json_sha256(contract_migrations),
            "base_schema_sha256": canonical_json_sha256(base_schema),
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
    chain, _ = load_tail_certificate()
    history = inspection.get("migration_history")
    if not isinstance(history, list):
        problems.append("full migration history is unavailable")
        return problems
    actual_tail = [
        {
            key: item[key]
            for key in ("version", "description", "success", "checksum_hex")
        }
        for item in history
        if item.get("version", 0) > rule["base_through"]
    ]
    certified_tail = [
        {
            "version": item["version"],
            "description": item["description"],
            "success": item["success"],
            "checksum_hex": item["checksum_hex"],
        }
        for item in chain["migrations"]
    ]
    if actual_tail != certified_tail:
        problems.append(
            "migration tail is not the exact independently reviewed 43-to-44 chain"
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
    match = CODEX_VERSION_PATTERN.search(raw)
    if result.returncode != 0 or not match:
        return {
            "ok": False,
            "version": None,
            "raw": raw,
            "error": f"codex --version exited {result.returncode} or was unparseable",
        }
    return {"ok": True, "version": match.group("version"), "raw": raw, "error": None}


def semver_parts(version: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    match = CODEX_VERSION_PATTERN.fullmatch(version)
    if not match:
        raise SafetyError(f"Invalid Codex semantic version: {version!r}")
    without_build = match.group("version").split("+", 1)[0]
    core, separator, prerelease = without_build.partition("-")
    major, minor, patch = core.split(".")
    return (int(major), int(minor), int(patch)), (
        tuple(prerelease.split(".")) if separator else None
    )


def semver_core(version: str) -> tuple[int, int, int]:
    return semver_parts(version)[0]


def compare_prerelease(left: tuple[str, ...] | None, right: tuple[str, ...] | None) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    for left_item, right_item in zip(left, right):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_item) > int(right_item) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_item > right_item else -1
    return (len(left) > len(right)) - (len(left) < len(right))


def semver_at_least(version: str, minimum: str) -> bool:
    version_core, version_pre = semver_parts(version)
    minimum_core, minimum_pre = semver_parts(minimum)
    if version_core != minimum_core:
        return version_core > minimum_core
    return compare_prerelease(version_pre, minimum_pre) >= 0


def ensure_plain_file(path: Path, *, chain_root: Path | None = None) -> Path:
    supplied = Path(os.path.abspath(path))
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    root = Path(supplied.anchor)
    try:
        relative = supplied.relative_to(root)
    except ValueError as exc:
        raise SafetyError(f"Executable has no stable absolute path: {supplied}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        metadata = current.lstat()
        if current.is_symlink() or int(
            getattr(metadata, "st_file_attributes", 0)
        ) & reparse_flag:
            raise SafetyError(f"Executable path contains a reparse point: {current}")
    resolved = canonical(supplied)
    if chain_root is not None and not is_below(resolved, canonical(chain_root)):
        raise SafetyError(f"Executable escaped its trusted path root: {resolved}")
    if not resolved.is_file():
        raise SafetyError(f"Expected a regular executable file: {resolved}")
    return resolved


def run_powershell_json(script: str, *, timeout: int = 20) -> Any:
    if os.name != "nt":
        raise SafetyError("Windows PowerShell is required for desktop runtime attestation")
    buffer = ctypes.create_unicode_buffer(32768)
    get_system_directory = ctypes.windll.kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    get_system_directory.restype = ctypes.c_uint
    length = get_system_directory(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise SafetyError("Windows system directory could not be resolved")
    system_directory = Path(buffer.value)
    executable = ensure_plain_file(
        system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        chain_root=system_directory,
    )
    prefix = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", prefix + script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyError(f"PowerShell runtime attestation failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SafetyError(f"PowerShell runtime attestation failed: {detail}")
    try:
        return json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise SafetyError("PowerShell runtime attestation returned invalid JSON") from exc


def discover_desktop_app_server() -> dict[str, Any]:
    script = r"""
$items = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -ieq 'codex.exe' -and $_.CommandLine -match '(^|\s)app-server(\s|$)'
} | ForEach-Object {
  $parent = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.ParentProcessId)
  [pscustomobject]@{
    process_id = [int]$_.ProcessId
    parent_process_id = [int]$_.ParentProcessId
    executable_path = [string]$_.ExecutablePath
    command_line = [string]$_.CommandLine
    parent_name = [string]$parent.Name
    parent_executable_path = [string]$parent.ExecutablePath
  }
})
ConvertTo-Json -Compress -Depth 4 -InputObject $items
"""
    value = run_powershell_json(script)
    if not isinstance(value, list):
        raise SafetyError("Desktop app-server inventory is incomplete")
    candidates: list[dict[str, Any]] = []
    windowsapps_prefix = (
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps" / "OpenAI.Codex_")
        .replace("/", "\\")
        .casefold()
    )
    for item in value:
        if not isinstance(item, dict):
            continue
        executable = item.get("executable_path")
        parent_name = item.get("parent_name")
        parent_executable = item.get("parent_executable_path")
        if (
            not isinstance(executable, str)
            or not isinstance(parent_name, str)
            or not isinstance(parent_executable, str)
        ):
            continue
        normalized = executable.replace("/", "\\").casefold()
        normalized_parent = parent_executable.replace("/", "\\").casefold()
        suffix = "\\app\\resources\\codex.exe"
        package_root = normalized[: -len(suffix)] if normalized.endswith(suffix) else ""
        if (
            parent_name.casefold() == "chatgpt.exe"
            and normalized.startswith(windowsapps_prefix)
            and package_root
            and normalized_parent == package_root + "\\app\\chatgpt.exe"
        ):
            candidates.append(item)
    if len(candidates) != 1:
        raise SafetyError(
            "Expected exactly one current OpenAI Codex desktop app-server process"
        )
    return candidates[0]


def authenticode_evidence(path: Path) -> dict[str, Any]:
    escaped = str(path).replace("'", "''")
    script = (
        f"$s=Get-AuthenticodeSignature -LiteralPath '{escaped}'; "
        "[pscustomobject]@{status=$s.Status.ToString(); "
        "subject=[string]$s.SignerCertificate.Subject; "
        "publisher=[string]$s.SignerCertificate.GetNameInfo("
        " [Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false); "
        "thumbprint=[string]$s.SignerCertificate.Thumbprint} | "
        "ConvertTo-Json -Compress"
    )
    value = run_powershell_json(script)
    if not isinstance(value, dict):
        raise SafetyError("Authenticode verification returned incomplete evidence")
    return value


def attest_desktop_runtime(codex_home: Path, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    evidence: dict[str, Any] = {
        "captured_at": now.isoformat(),
        "ok": False,
        "reason": None,
        "desktop_process": None,
        "bundled_backend": None,
        "mirror": None,
        "cli": None,
    }
    try:
        process = discover_desktop_app_server()
        bundled = ensure_plain_file(Path(str(process["executable_path"])))
        mirror = ensure_plain_file(
            codex_home / "plugins" / ".plugin-appserver" / "codex.exe",
            chain_root=codex_home,
        )
        bundled_stat = bundled.stat()
        mirror_stat = mirror.stat()
        bundled_identity = (
            bundled_stat.st_dev,
            bundled_stat.st_ino,
            bundled_stat.st_size,
            bundled_stat.st_mtime_ns,
        )
        mirror_identity = (
            mirror_stat.st_dev,
            mirror_stat.st_ino,
            mirror_stat.st_size,
            mirror_stat.st_mtime_ns,
        )
        if bundled_stat.st_size != mirror_stat.st_size:
            raise SafetyError("Desktop backend mirror size differs from the running backend")
        bundled_hash = sha256_file(bundled)
        mirror_hash = sha256_file(mirror)
        if bundled_hash != mirror_hash:
            raise SafetyError("Desktop backend mirror hash differs from the running backend")
        signature = authenticode_evidence(mirror)
        if (
            signature.get("status") != "Valid"
            or signature.get("publisher") != "OpenAI OpCo, LLC"
            or not re.fullmatch(r"[0-9A-Fa-f]{40}", str(signature.get("thumbprint", "")))
        ):
            raise SafetyError("Desktop backend mirror lacks a valid OpenAI signature")
        version = read_codex_version(str(mirror))
        if not version["ok"]:
            raise SafetyError(str(version["error"]))
        final_bundled = ensure_plain_file(Path(str(process["executable_path"])))
        final_mirror = ensure_plain_file(
            codex_home / "plugins" / ".plugin-appserver" / "codex.exe",
            chain_root=codex_home,
        )
        final_bundled_stat = final_bundled.stat()
        final_mirror_stat = final_mirror.stat()
        if (
            final_bundled != bundled
            or final_mirror != mirror
            or (
                final_bundled_stat.st_dev,
                final_bundled_stat.st_ino,
                final_bundled_stat.st_size,
                final_bundled_stat.st_mtime_ns,
            )
            != bundled_identity
            or (
                final_mirror_stat.st_dev,
                final_mirror_stat.st_ino,
                final_mirror_stat.st_size,
                final_mirror_stat.st_mtime_ns,
            )
            != mirror_identity
            or sha256_file(final_bundled) != bundled_hash
            or sha256_file(final_mirror) != mirror_hash
        ):
            raise SafetyError("Desktop runtime files changed during attestation")
        evidence.update(
            {
                "ok": True,
                "desktop_process": process,
                "bundled_backend": {
                    "path": str(bundled),
                    "bytes": bundled_stat.st_size,
                    "sha256": bundled_hash,
                    "mtime_ns": bundled_stat.st_mtime_ns,
                },
                "mirror": {
                    "path": str(mirror),
                    "bytes": mirror_stat.st_size,
                    "sha256": mirror_hash,
                    "mtime_ns": mirror_stat.st_mtime_ns,
                    "authenticode": signature,
                },
                "cli": version,
            }
        )
    except (OSError, SafetyError) as exc:
        evidence["reason"] = str(exc)
    return evidence


def inspect_preflight_database(database: Path, required: list[dict[str, Any]]) -> dict[str, Any]:
    connection = readonly_connection(database)
    try:
        migration_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations'"
        ).fetchone()
        history: list[dict[str, Any]] = []
        if migration_table:
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
                ).fetchall()
            ]
        required_versions = {item["version"] for item in required}
        anchors = [item for item in history if item["version"] in required_versions]
        placeholders = ",".join("?" for _ in KNOWN_OBJECTS)
        present = sorted(
            str(row[0])
            for row in connection.execute(
                f"SELECT name FROM sqlite_master WHERE name IN ({placeholders})",
                sorted(KNOWN_OBJECTS),
            ).fetchall()
        )
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            f"AND name NOT IN ({placeholders}) "
            "ORDER BY type, name, tbl_name",
            sorted(KNOWN_OBJECTS),
        ).fetchall()
        base_schema = [
            {
                "type": str(row[0]),
                "name": str(row[1]),
                "table": str(row[2]),
                "sql": None if row[3] is None else str(row[3]),
            }
            for row in schema_rows
        ]
        return {
            "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            "migration_table_present": bool(migration_table),
            "migration_count": len(history),
            "max_successful_migration": max(
                (item["version"] for item in history if item["success"] == 1),
                default=None,
            ),
            "failed_migrations": [item for item in history if item["success"] != 1],
            "required_migrations": anchors,
            "migration_history_sha256": canonical_json_sha256(history),
            "base_schema_sha256": canonical_json_sha256(base_schema),
            "compatibility_objects_present": present,
        }
    finally:
        connection.close()


def required_migration_problems(
    inspection: dict[str, Any], required: list[dict[str, Any]], *, quick_check: bool
) -> list[str]:
    problems: list[str] = []
    if not inspection.get("migration_table_present"):
        return ["_sqlx_migrations is missing"]
    if quick_check and inspection.get("quick_check") != "ok":
        problems.append(f"state quick_check is {inspection.get('quick_check')!r}")
    if inspection.get("failed_migrations"):
        problems.append("failed migration rows are present")
    source = inspection.get("required_migrations", inspection.get("migrations", []))
    rows = {item["version"]: item for item in source if isinstance(item, dict)}
    for expected in required:
        actual = rows.get(expected["version"])
        if actual is None:
            problems.append(f"required migration {expected['version']} is missing")
        elif any(actual.get(key) != expected[key] for key in ("description", "success", "checksum_hex")):
            problems.append(f"migration {expected['version']} differs from the reviewed anchor")
    return problems


def preflight(
    codex_home: Path,
    profile_path: Path = DEFAULT_PROFILE,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    home = canonical(codex_home)
    state_db = require_live_state_database(home, "state_5.sqlite")
    document, _, resolved_profile = load_profiles(profile_path)
    native = document["native_delete"]
    runtime = attest_desktop_runtime(home, now=now)
    database = inspect_preflight_database(state_db, native["required_migrations"])
    report: dict[str, Any] = {
        "schema_version": 2,
        "operation": "preflight",
        "checked_at": now.isoformat(),
        "codex_home": str(home),
        "state_database": str(state_db),
        "profile_file": str(resolved_profile),
        "profile_sha256": sha256_file(resolved_profile),
        "runtime": runtime,
        "database": database,
        "native_delete": False,
        "allow_expensive_inventory": False,
        "recommended_codex_exe": None,
        "decision": "unsupported_update_required",
        "reasons": [],
        "next_action": "Keep automation paused and inspect the preflight evidence.",
    }
    review_after = parse_date(document["review_after"], "document review_after")
    if now.date() > review_after:
        report["decision"] = "stale_profile_update_required"
        report["reasons"].append(f"review deadline {review_after.isoformat()} has passed")
    elif not runtime["ok"]:
        report["reasons"].append(str(runtime["reason"]))
    else:
        report["reasons"].extend(
            required_migration_problems(
                database, native["required_migrations"], quick_check=False
            )
        )
        version = runtime["cli"]["version"]
        if not semver_at_least(version, native["minimum_codex_cli_version"]):
            report["reasons"].append(
                f"matched desktop runtime {version} predates the native delete fix"
            )
        if database["compatibility_objects_present"]:
            report["reasons"].append("temporary compatibility objects are present")
        if not report["reasons"]:
            report.update(
                {
                    "native_delete": True,
                    "allow_expensive_inventory": True,
                    "recommended_codex_exe": runtime["mirror"]["path"],
                    "decision": "canary_required",
                    "next_action": (
                        "Proceed to fresh activity inventory and external online backups, then use "
                        "only the recommended matched runtime for one official delete canary."
                    ),
                }
            )
    report["condition_key"] = canonical_json_sha256(
        {
            "decision": report["decision"],
            "native_delete": report["native_delete"],
            "runtime_version": runtime.get("cli", {}).get("version") if runtime.get("cli") else None,
            "runtime_sha256": runtime.get("mirror", {}).get("sha256") if runtime.get("mirror") else None,
            "migration_history_sha256": database["migration_history_sha256"],
            "schema_version": database["schema_version"],
            "base_schema_sha256": database["base_schema_sha256"],
            "compatibility_objects_present": database["compatibility_objects_present"],
            "reasons": report["reasons"],
        }
    )
    return report


def validate_runtime_evidence(
    path: Path,
    document: dict[str, Any],
    codex_home: Path,
    profile_path: Path,
    now: dt.datetime,
) -> tuple[dict[str, Any], list[str], Path]:
    resolved = ensure_external_path(path, codex_home, must_exist=True)
    evidence = load_json(resolved)
    problems: list[str] = []
    try:
        captured = parse_datetime(evidence.get("checked_at"), "runtime evidence checked_at")
        age = (now - captured).total_seconds() / 60
        maximum = int(document["policy"]["runtime_evidence_max_age_minutes"])
        if age < -1 or age > maximum:
            problems.append(f"runtime evidence is outside the {maximum}-minute freshness window")
    except (SafetyError, KeyError, TypeError, ValueError) as exc:
        problems.append(str(exc))
    if (
        evidence.get("schema_version") != 2
        or evidence.get("operation") != "preflight"
        or evidence.get("decision") != "canary_required"
        or evidence.get("native_delete") is not True
        or evidence.get("allow_expensive_inventory") is not True
    ):
        problems.append("runtime evidence is not an allowed native preflight result")
    try:
        if canonical(Path(str(evidence.get("codex_home")))) != codex_home:
            problems.append("runtime evidence belongs to a different CodexHome")
    except SafetyError as exc:
        problems.append(str(exc))
    fresh = preflight(codex_home, profile_path, now=now)
    for key in ("condition_key", "recommended_codex_exe", "profile_sha256"):
        if evidence.get(key) != fresh.get(key):
            problems.append(f"runtime evidence {key} changed since preflight")
    if fresh.get("decision") != "canary_required" or fresh.get("native_delete") is not True:
        problems.append("fresh runtime preflight no longer permits native canary")
    return evidence, problems, resolved


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
    app_error = data.get("app_server_error")
    if not isinstance(app_error, dict) or app_error.get("code") != -32603:
        problems.append("app-server error code differs from the reviewed failure")
        app_error = {}
    message = app_error.get("message")
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
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        thread_id,
    ):
        problems.append("canary_thread_id is missing or invalid")
    elif data.get("official_cli_command") != f"codex delete --force {thread_id}":
        problems.append("official CLI command is not bound to the recorded canary")
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
        summary_time = dt.datetime.fromtimestamp(resolved.stat().st_mtime, dt.timezone.utc)
        if abs((summary_time - created_at).total_seconds()) > 300:
            problems.append("backup summary file timestamp differs from its created_at")
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
        source: Path | None = None
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
            backup_metadata = backup.lstat()
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not backup.is_file()
                or backup.is_symlink()
                or int(getattr(backup_metadata, "st_file_attributes", 0)) & reparse_flag
            ):
                problems.append(f"{name} backup is not a regular file")
                continue
            if source is not None and os.path.samefile(source, backup):
                problems.append(f"{name} backup is a hardlink to the live database")
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
    runtime_evidence: Path | None = None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    codex_home = canonical(codex_home)
    state_db = require_live_state_database(codex_home, "state_5.sqlite")
    document, profiles, resolved_profile = load_profiles(profile_path)
    version = (
        {"ok": False, "version": None, "raw": None, "error": "runtime evidence pending"}
        if runtime_evidence is not None
        else read_codex_version(codex_exe)
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "operation": "diagnose",
        "checked_at": now.isoformat(),
        "codex_home": str(codex_home),
        "state_database": str(state_db),
        "profile_file": str(resolved_profile),
        "profile_sha256": sha256_file(resolved_profile),
        "tail_certificates_sha256": None,
        "cli": version,
        "matched_profile_id": None,
        "database": None,
        "database_identity": None,
        "failure_evidence": None,
        "backup_summary": None,
        "runtime_evidence": None,
        "decision": "unsafe_stop",
        "reasons": [],
        "next_action": "Stop deletion and inspect the diagnostic evidence.",
    }

    if runtime_evidence is not None:
        evidence, evidence_problems, evidence_path = validate_runtime_evidence(
            runtime_evidence, document, codex_home, resolved_profile, now
        )
        recommended = evidence.get("recommended_codex_exe")
        try:
            if not isinstance(recommended, str):
                raise SafetyError("runtime evidence has no recommended executable")
            if codex_exe is not None:
                supplied = shutil.which(codex_exe) or codex_exe
                if canonical(Path(supplied)) != canonical(Path(recommended)):
                    raise SafetyError("diagnosis executable differs from runtime evidence")
            current_runtime = attest_desktop_runtime(codex_home, now=now)
            if not current_runtime["ok"]:
                raise SafetyError(str(current_runtime["reason"]))
            recorded_runtime = evidence.get("runtime", {})
            for section, key in (
                ("bundled_backend", "path"),
                ("bundled_backend", "sha256"),
                ("mirror", "path"),
                ("mirror", "sha256"),
                ("cli", "version"),
            ):
                if current_runtime.get(section, {}).get(key) != recorded_runtime.get(
                    section, {}
                ).get(key):
                    raise SafetyError(f"matched runtime {section}.{key} changed after preflight")
            version = current_runtime["cli"]
            recommended = current_runtime["mirror"]["path"]
        except (OSError, SafetyError) as exc:
            evidence_problems.append(str(exc))
        report["cli"] = version
        report["runtime_evidence"] = {
            "path": str(evidence_path),
            "sha256": sha256_file(evidence_path),
            "condition_key": evidence.get("condition_key"),
            "valid": not evidence_problems,
            "problems": evidence_problems,
        }
        profile = profiles[0]
        inspection = inspect_database(state_db, profile)
        report["database"] = inspection
        report["database_identity"] = database_identity(state_db)
        report["matched_profile_id"] = "native-desktop-runtime"
        native_problems = required_migration_problems(
            inspection, document["native_delete"]["required_migrations"], quick_check=True
        )
        recorded_database = evidence.get("database", {})
        if inspection["migration_contract_sha256"] != recorded_database.get(
            "migration_history_sha256"
        ):
            native_problems.append("migration ledger changed after the fresh runtime gate")
        if inspection["base_schema_sha256"] != recorded_database.get("base_schema_sha256"):
            native_problems.append("base schema changed after the fresh runtime gate")
        if inspection["schema_version"] != recorded_database.get("schema_version"):
            native_problems.append("SQLite schema_version changed after the fresh runtime gate")
        state, object_problems = object_state(inspection, profile)
        if state != "absent":
            native_problems.extend(object_problems or ["temporary compatibility objects are present"])
        if failure_evidence is not None:
            native_problems.append(
                "a native-runtime canary failure is never eligible for the legacy database shim"
            )
        if backup_summary is None:
            native_problems.append("a fresh external backup summary is required before canary")
        else:
            try:
                _, backup_problems, backup_path = validate_backup_summary(
                    backup_summary, document, codex_home, now
                )
                report["backup_summary"] = {
                    "path": str(backup_path),
                    "sha256": sha256_file(backup_path),
                    "valid": not backup_problems,
                    "problems": backup_problems,
                }
                native_problems.extend(backup_problems)
            except (OSError, SafetyError, sqlite3.Error) as exc:
                native_problems.append(f"backup summary could not be validated: {exc}")
        if evidence_problems or native_problems:
            report["decision"] = "unsafe_stop"
            report["reasons"].extend(evidence_problems + native_problems)
            report["next_action"] = (
                "Stop the batch. Preserve the native canary error unchanged and review the new failure; "
                "do not install legacy compatibility objects."
            )
            return report
        report["decision"] = "canary_required"
        report["reasons"].append(
            "fresh signed desktop runtime pairing and current database anchors match"
        )
        report["recommended_codex_exe"] = recommended
        report["native_delete"] = True
        report["next_action"] = (
            "Use only the matched recommended executable for exactly one official delete canary. "
            "Any failure stops the batch without a compatibility install."
        )
        return report

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
    _, resolved_tail = load_tail_certificate()
    report["tail_certificates_sha256"] = sha256_file(resolved_tail)
    inspection = inspect_database(state_db, profile)
    report["database"] = inspection
    report["database_identity"] = database_identity(state_db)

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
        report["decision"] = "unsupported_update_required"
        report["reasons"].append(
            "legacy 0.142.2 is recovery-only; it may not start a new delete canary"
        )
        report["next_action"] = (
            "Return to desktop-runtime preflight and use the matched native runtime. "
            "Only an already existing exact legacy partial-deletion incident may use this fallback."
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


def require_bound_artifacts(
    bindings: Iterable[tuple[str, Path, str]], *, phase: str
) -> None:
    for label, path, expected_hash in bindings:
        if sha256_file(path) != expected_hash:
            raise SafetyError(f"{label} changed {phase}")


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
    if sha256_file(DEFAULT_TAIL_CERTIFICATES) != report["tail_certificates_sha256"]:
        raise SafetyError("Tail certificates changed after diagnosis")
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
        raise SafetyError(
            "Backup revalidation failed before schema verification: "
            + "; ".join(backup_problems)
        )
    if database_identity(state_db) != report["database_identity"]:
        raise SafetyError("State database file identity changed after diagnosis")
    install_bindings = [
        ("compatibility profile", profile_path, report["profile_sha256"]),
        (
            "tail certificates",
            DEFAULT_TAIL_CERTIFICATES,
            report["tail_certificates_sha256"],
        ),
        (
            "canary failure evidence",
            Path(report["failure_evidence"]["path"]),
            report["failure_evidence"]["sha256"],
        ),
        (
            "backup summary",
            Path(report["backup_summary"]["path"]),
            report["backup_summary"]["sha256"],
        ),
    ]

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
        "database_identity": report["database_identity"],
        "codex_cli_version": report["cli"]["version"],
        "migration_max_successful": report["database"]["max_successful_migration"],
        "migration_history_sha256": report["database"]["migration_history_sha256"],
        "migration_contract_sha256": report["database"]["migration_contract_sha256"],
        "base_schema_sha256": report["database"]["base_schema_sha256"],
        "tail_certificates_sha256": report["tail_certificates_sha256"],
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
        if database_identity(state_db) != journal["database_identity"]:
            raise SafetyError("State database file identity changed before install")
        require_bound_artifacts(install_bindings, phase="before install")
        inspection = inspect_database(state_db, profile, connection)
        migration_problems = migration_matches(inspection, profile)
        state, object_problems = object_state(inspection, profile)
        if inspection["migration_history_sha256"] != report["database"]["migration_history_sha256"]:
            migration_problems.append("full migration ledger changed after diagnosis")
        if inspection["base_schema_sha256"] != report["database"]["base_schema_sha256"]:
            object_problems.append("base schema changed after diagnosis")
        if inspection["schema_version"] != report["database"]["schema_version"]:
            object_problems.append("SQLite schema_version changed after diagnosis")
        if migration_problems or state != "absent" or object_problems:
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
        if after["migration_history_sha256"] != inspection["migration_history_sha256"]:
            raise SafetyError("Migration history changed during compatibility install")
        if after["base_schema_sha256"] != inspection["base_schema_sha256"]:
            raise SafetyError("Base schema changed during compatibility install")
        if after["quick_check"] != "ok":
            raise SafetyError("State database quick_check failed after compatibility install")
        require_bound_artifacts(install_bindings, phase="during install")
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
    if not isinstance(install.get("migration_max_successful"), int):
        problems.append("install result migration maximum is missing")
    for key in (
        "migration_history_sha256",
        "migration_contract_sha256",
        "base_schema_sha256",
    ):
        if not isinstance(install.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", install[key]):
            problems.append(f"install result {key} is missing or invalid")
    if install.get("tail_certificates_sha256") != sha256_file(DEFAULT_TAIL_CERTIFICATES):
        problems.append("tail certificates changed after installation")
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
    if inspection["migration_history_sha256"] != install.get("migration_history_sha256"):
        migration_problems.append("full migration ledger changed after installation")
    if inspection["migration_contract_sha256"] != install.get("migration_contract_sha256"):
        migration_problems.append("migration contract changed after installation")
    if inspection["max_successful_migration"] != install.get("migration_max_successful"):
        migration_problems.append("migration maximum changed after installation")
    if inspection["base_schema_sha256"] != install.get("base_schema_sha256"):
        object_problems.append("base schema changed after installation")
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
    remove_bindings = [
        ("install result", install_path, sha256_file(install_path)),
        ("compatibility profile", profile_path, install["profile_sha256"]),
        (
            "tail certificates",
            DEFAULT_TAIL_CERTIFICATES,
            install["tail_certificates_sha256"],
        ),
        (
            "canary failure evidence",
            Path(install["failure_evidence"]),
            install["failure_evidence_sha256"],
        ),
        (
            "backup summary",
            Path(install["backup_summary"]),
            install["backup_summary_sha256"],
        ),
    ]

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
        "migration_max_successful": install["migration_max_successful"],
        "migration_history_sha256": install["migration_history_sha256"],
        "migration_contract_sha256": install["migration_contract_sha256"],
        "base_schema_sha256": install["base_schema_sha256"],
        "tail_certificates_sha256": install["tail_certificates_sha256"],
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
        if database_identity(state_db) != install["database_identity"]:
            raise SafetyError("State database file identity changed before removal")
        require_bound_artifacts(remove_bindings, phase="before removal")
        live = inspect_database(state_db, profile, connection)
        live_migration_problems = migration_matches(live, profile)
        live_state, live_object_problems = object_state(live, profile)
        if live_migration_problems or live_state != "exact" or live_object_problems:
            raise SafetyError("Live state changed before compatibility removal")
        if live["migration_history_sha256"] != install["migration_history_sha256"]:
            raise SafetyError("Full migration ledger changed before compatibility removal")
        if live["base_schema_sha256"] != install["base_schema_sha256"]:
            raise SafetyError("Base schema changed before compatibility removal")
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
        if after["migration_history_sha256"] != install["migration_history_sha256"]:
            raise SafetyError("Migration history changed during compatibility removal")
        if after["base_schema_sha256"] != install["base_schema_sha256"]:
            raise SafetyError("Base schema changed during compatibility removal")
        if after["quick_check"] != "ok":
            raise SafetyError("State database quick_check failed after compatibility removal")
        require_bound_artifacts(remove_bindings, phase="during removal")
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

    preflight_parser = subparsers.add_parser(
        "preflight", help="Lightweight read-only desktop-runtime and migration gate"
    )
    add_common_arguments(preflight_parser)
    preflight_parser.add_argument("--output")

    diagnose_parser = subparsers.add_parser("diagnose", help="Read-only compatibility diagnosis")
    add_common_arguments(diagnose_parser)
    diagnose_parser.add_argument("--codex-exe")
    diagnose_parser.add_argument("--failure-evidence")
    diagnose_parser.add_argument("--backup-summary")
    diagnose_parser.add_argument("--runtime-evidence")
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
        if args.command == "preflight":
            report = preflight(Path(args.codex_home), Path(args.profile_file))
            output: Path | None = None
            if args.output:
                output = prepare_output_path(Path(args.output), canonical(Path(args.codex_home)))
            emit(report, output)
            return DECISION_EXIT_CODES.get(report["decision"], 4)
        if args.command == "diagnose":
            report = diagnose(
                Path(args.codex_home),
                Path(args.profile_file),
                args.codex_exe,
                Path(args.failure_evidence) if args.failure_evidence else None,
                Path(args.backup_summary) if args.backup_summary else None,
                Path(args.runtime_evidence) if args.runtime_evidence else None,
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
                "not_requested"
                if getattr(args, "command", None) in {"preflight", "diagnose"}
                else "unknown; inspect live state"
            ),
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
