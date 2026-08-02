#!/usr/bin/env python3
"""Safely check for or fast-forward to a trusted skill release.

This updater changes only the local skill Git checkout. It never upgrades the
Codex CLI, opens a Codex database, invokes a delete API, pushes a branch, or
merges a pull request.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REMOTE = "https://github.com/skills-qweer/codex-storage-cleanup"
UPDATE_TOKEN = "UPDATE_CODEX_STORAGE_SKILL"
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
REVIEWED_MIGRATION_SPECS = {
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
REVIEWED_TAIL_CHAIN_ID = "codex-state-42-to-44-delete-unrelated-v1"
REVIEWED_DELETE_CONTRACT_ID = "codex-0.142.2-delete-threads-strict-agent-jobs-v1"
REVIEWED_UPDATED_AT = "2026-08-03"
REVIEWED_REVIEW_AFTER = "2026-11-01"
REVIEWED_TAIL_MIGRATIONS = {
    43: {
        "description": "threads is pinned",
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


class UpdateError(RuntimeError):
    """Raised when a trusted update precondition fails."""


def run_git(
    args: list[str],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise UpdateError(f"git {' '.join(args)} failed: {detail}")
    return result


def normalize_remote(value: str) -> str:
    remote = value.strip().replace("\\", "/")
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote[len("git@github.com:") :]
    elif remote.startswith("ssh://git@github.com/"):
        remote = "https://github.com/" + remote[len("ssh://git@github.com/") :]
    remote = remote.rstrip("/")
    if remote.casefold().endswith(".git"):
        remote = remote[:-4]
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)", remote, re.IGNORECASE)
    if not match:
        return remote
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


def current_operation_markers(repo: Path) -> list[str]:
    git_dir_text = run_git(["rev-parse", "--git-dir"], cwd=repo).stdout.strip()
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()
    markers = {
        "merge": git_dir / "MERGE_HEAD",
        "cherry-pick": git_dir / "CHERRY_PICK_HEAD",
        "revert": git_dir / "REVERT_HEAD",
        "rebase-merge": git_dir / "rebase-merge",
        "rebase-apply": git_dir / "rebase-apply",
        "bisect": git_dir / "BISECT_LOG",
    }
    return [name for name, path in markers.items() if path.exists()]


def inspect_local_repo(repo: Path) -> dict[str, Any]:
    root = Path(run_git(["rev-parse", "--show-toplevel"], cwd=repo).stdout.strip()).resolve()
    if root != repo.resolve():
        raise UpdateError(f"Updater is not running from the expected repository root: {root}")
    origin_raw = run_git(["remote", "get-url", "origin"], cwd=repo).stdout.strip()
    branch = run_git(["branch", "--show-current"], cwd=repo).stdout.strip()
    upstream_result = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=repo,
        check=False,
    )
    upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    status = run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=repo
    ).stdout.splitlines()
    markers = current_operation_markers(repo)
    ssl_verify = run_git(
        ["config", "--bool", "--get", "http.sslVerify"], cwd=repo, check=False
    )
    ssl_disabled = (
        os.environ.get("GIT_SSL_NO_VERIFY", "").casefold() in {"1", "true", "yes"}
        or (ssl_verify.returncode == 0 and ssl_verify.stdout.strip().casefold() == "false")
    )
    return {
        "root": str(root),
        "origin_raw": origin_raw,
        "origin_normalized": normalize_remote(origin_raw),
        "branch": branch,
        "upstream": upstream,
        "head": run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip(),
        "dirty_entries": status,
        "operation_markers": markers,
        "tls_verification_disabled": ssl_disabled,
    }


def local_update_blockers(state: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if state["origin_normalized"].casefold() != EXPECTED_REMOTE.casefold():
        blockers.append("origin URL is not the allow-listed repository")
    if EXPECTED_REMOTE.startswith("https://github.com/") and not state[
        "origin_raw"
    ].casefold().startswith("https://github.com/"):
        blockers.append("automatic updates require the allow-listed HTTPS origin")
    if state["branch"] != "main":
        blockers.append("current branch is not main")
    if state["upstream"] != "origin/main":
        blockers.append("main does not track origin/main")
    if state["dirty_entries"]:
        blockers.append("working tree has tracked, staged, or untracked changes")
    if state["operation_markers"]:
        blockers.append("a Git operation is already in progress")
    if state.get("tls_verification_disabled") and state["origin_raw"].casefold().startswith(
        "https://"
    ):
        blockers.append("Git HTTPS certificate verification is disabled")
    return blockers


def profile_lifecycle(repo: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    path = repo / "references" / "subagent-delete-compatibility.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        review_dates = [dt.date.fromisoformat(document["review_after"])]
        review_dates.extend(
            dt.date.fromisoformat(profile["review_after"])
            for profile in document["profiles"]
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Invalid local compatibility profile: {exc}") from exc
    earliest = min(review_dates)
    return {
        "path": str(path),
        "review_after": earliest.isoformat(),
        "stale": today > earliest,
    }


def incident_blockers(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        data = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"incident evidence is unreadable: {exc}"]
    blockers: list[str] = []
    if data.get("schema_version") != 1:
        return ["incident evidence schema is unknown"]
    partial = data.get("partial_deletion")
    required_partial = {
        "rollout_file_absent",
        "state_thread_row_present",
        "spawn_edge_present",
        "state_quick_check",
    }
    if not isinstance(partial, dict) or not required_partial.issubset(partial):
        return ["incident evidence partial-deletion shape is unknown"]
    for key in required_partial - {"state_quick_check"}:
        if not isinstance(partial.get(key), bool):
            return ["incident evidence partial-deletion values are invalid"]
    if any(partial.get(key) is True for key in required_partial - {"state_quick_check"}):
        blockers.append("incident evidence records a partial deletion")
    if partial.get("state_quick_check") != "ok":
        blockers.append("incident evidence records a failed or unknown state quick_check")
    safety = data.get("safety_state")
    required_safety = {
        "bulk_delete_started",
        "other_authorized_roots_deleted",
        "database_backups_created",
        "automatic_compatibility_workaround_applied",
    }
    if not isinstance(safety, dict) or not required_safety.issubset(safety):
        return ["incident evidence safety-state shape is unknown"]
    for key in (
        "bulk_delete_started",
        "database_backups_created",
        "automatic_compatibility_workaround_applied",
    ):
        if not isinstance(safety.get(key), bool):
            return ["incident evidence safety-state values are invalid"]
    if safety.get("bulk_delete_started") is True:
        blockers.append("incident evidence records that bulk deletion started")
    deleted_roots = safety.get("other_authorized_roots_deleted")
    if type(deleted_roots) is not int or deleted_roots < 0:
        return ["incident evidence deleted-root count is invalid"]
    if deleted_roots > 0:
        blockers.append("incident evidence records deleted authorized roots")
    if safety.get("automatic_compatibility_workaround_applied") is True:
        blockers.append("incident evidence records an installed workaround")
    return blockers


def compatibility_diagnosis(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        resolved = path.resolve(strict=True)
        raw = resolved.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"compatibility diagnosis is unreadable: {exc}") from exc
    allowed = {
        "canary_required",
        "known_workaround_eligible",
        "compat_installed",
        "stale_profile_update_required",
        "unsupported_update_required",
        "unsafe_stop",
    }
    if (
        data.get("schema_version") != 1
        or data.get("operation") != "diagnose"
        or data.get("decision") not in allowed
    ):
        raise UpdateError("compatibility diagnosis schema or decision is unknown")
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "decision": data["decision"],
        "profile_sha256": data.get("profile_sha256"),
    }


def remote_main_sha(repo: Path) -> str:
    result = run_git(["ls-remote", "--exit-code", "origin", "refs/heads/main"], cwd=repo)
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not re.fullmatch(
        r"[0-9a-f]{40,64}", fields[0]
    ):
        raise UpdateError("origin/main returned an unexpected ls-remote response")
    return fields[0]


def remote_relation(repo: Path, local_sha: str, remote_sha: str) -> str:
    if local_sha == remote_sha:
        return "up_to_date"
    exists = run_git(["cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=repo, check=False)
    if exists.returncode != 0:
        return "update_available_unfetched"
    ancestor = run_git(
        ["merge-base", "--is-ancestor", local_sha, remote_sha], cwd=repo, check=False
    )
    if ancestor.returncode == 0:
        return "fast_forward_available"
    if ancestor.returncode == 1:
        return "diverged_or_rewritten"
    raise UpdateError("git merge-base failed unexpectedly")


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).casefold()


def require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise UpdateError(f"{label} has unknown, missing, or self-approving fields")
    return value


def load_static_json(path: Path, label: str) -> dict[str, Any]:
    metadata = path.lstat()
    if path.is_symlink() or int(getattr(metadata, "st_file_attributes", 0)) & 0x400:
        raise UpdateError(f"{label} must not be a symlink or reparse point")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise UpdateError(f"{label} must be a JSON object")
    return document


def validate_required_migrations(value: Any, label: str) -> None:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, dict) for item in value)
        or [item.get("version") for item in value] != [14, 15, 42]
    ):
        raise UpdateError(f"{label} must contain only reviewed migrations 14, 15, and 42")
    for item in value:
        item = require_exact_keys(
            item,
            {"version", "description", "success", "checksum_hex"},
            f"{label} entry",
        )
        version = item["version"]
        description, checksum = REVIEWED_MIGRATION_SPECS[version]
        if item != {
            "version": version,
            "description": description,
            "success": 1,
            "checksum_hex": checksum,
        }:
            raise UpdateError(f"{label} fingerprint differs for migration {version}")


def validate_profile_file(path: Path) -> dict[str, Any]:
    document = load_static_json(path, "compatibility profile")
    require_exact_keys(
        document,
        {"schema_version", "updated_at", "review_after", "policy", "native_delete", "profiles"},
        "compatibility profile",
    )
    if document.get("schema_version") != 2 or not isinstance(document.get("profiles"), list):
        raise UpdateError("compatibility profile has an unsupported shape")
    dt.date.fromisoformat(document["updated_at"])
    dt.date.fromisoformat(document["review_after"])
    if (
        document["updated_at"] != REVIEWED_UPDATED_AT
        or document["review_after"] != REVIEWED_REVIEW_AFTER
    ):
        raise UpdateError("compatibility profile review dates are not the reviewed release")
    expected_policy = {
        "version_alone_is_never_sufficient": True,
        "backup_max_age_minutes": 60,
        "failure_evidence_max_age_hours": 24,
        "compat_install_max_age_hours": 24,
        "runtime_evidence_max_age_minutes": 15,
        "unknown_combinations": "stop_and_update_skill",
        "automatic_database_workaround": False,
        "automatic_repository_merge": False,
    }
    if document["policy"] != expected_policy:
        raise UpdateError("compatibility profile policy weakens a reviewed fail-closed control")

    native = require_exact_keys(
        document["native_delete"],
        {
            "minimum_codex_cli_version",
            "requires_desktop_runtime_hash_match",
            "requires_valid_openai_signature",
            "required_migrations",
            "compatibility_objects",
            "official_canary_required",
            "workaround_allowed",
            "sources",
        },
        "native delete policy",
    )
    validate_required_migrations(native["required_migrations"], "native delete migrations")
    expected_native_sources = [
        {
            "kind": "official_drop_and_delete_fix",
            "url": "https://github.com/openai/codex/commit/687f05cb946d10c96f90dd7ce82e11465c6e20a7",
        },
        {
            "kind": "official_fixed_delete_implementation",
            "url": (
                "https://github.com/openai/codex/blob/rust-v0.146.0-alpha.9.2/"
                "codex-rs/state/src/runtime/threads.rs#L1042-L1090"
            ),
        },
    ]
    if (
        native["minimum_codex_cli_version"] != "0.145.0"
        or native["requires_desktop_runtime_hash_match"] is not True
        or native["requires_valid_openai_signature"] is not True
        or native["compatibility_objects"] != "must_be_absent"
        or native["official_canary_required"] is not True
        or native["workaround_allowed"] is not False
        or native["sources"] != expected_native_sources
    ):
        raise UpdateError("native delete policy differs from the reviewed runtime contract")

    if len(document["profiles"]) != 1:
        raise UpdateError("compatibility profile must contain exactly one reviewed legacy fallback")
    profile = require_exact_keys(
        document["profiles"][0],
        {
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
        },
        "legacy compatibility profile",
    )
    dt.date.fromisoformat(profile["validated_at"])
    dt.date.fromisoformat(profile["review_after"])
    if (
        profile["validated_at"] != REVIEWED_UPDATED_AT
        or profile["review_after"] != REVIEWED_REVIEW_AFTER
    ):
        raise UpdateError("legacy profile review dates are not the reviewed release")
    if (
        profile["id"]
        != "codex-0.142.2-state-migration-42-tail44-missing-agent-jobs-v2"
        or profile["delete_contract_id"] != REVIEWED_DELETE_CONTRACT_ID
        or profile["status"] != "manual_compat_only"
        or profile["codex_cli_versions"] != ["0.142.2"]
        or profile["state_database"] != "state_5.sqlite"
    ):
        raise UpdateError("legacy compatibility profile identity is unsupported")

    migration = require_exact_keys(
        profile["migration"],
        {"base_through", "required", "tail_chain_id"},
        "legacy migration policy",
    )
    if migration["base_through"] != 42 or migration["tail_chain_id"] != REVIEWED_TAIL_CHAIN_ID:
        raise UpdateError("legacy migration policy is not bound to the reviewed 42-to-44 tail")
    validate_required_migrations(migration["required"], "legacy required migrations")

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
    if profile["failure_fingerprint"] != expected_failure:
        raise UpdateError("legacy failure fingerprint differs from the reviewed incident")

    objects = profile["objects"]
    if (
        not isinstance(objects, list)
        or len(objects) != len(REVIEWED_OBJECT_SPECS)
        or [item.get("name") for item in objects if isinstance(item, dict)]
        != list(REVIEWED_OBJECT_SPECS)
    ):
        raise UpdateError("compatibility profile object set is invalid")
    for item in objects:
        item = require_exact_keys(
            item,
            {"name", "type", "ddl", "normalized_sha256"},
            "legacy compatibility object",
        )
        name = item["name"]
        expected_type, expected_hash = REVIEWED_OBJECT_SPECS[name]
        if item["type"] != expected_type:
            raise UpdateError(f"compatibility profile changes the type of {name}")
        ddl = item["ddl"]
        if not isinstance(ddl, str) or ";" in ddl or "--" in ddl or "/*" in ddl:
            raise UpdateError(f"compatibility profile SQL is unsafe for {name}")
        digest = hashlib.sha256(normalize_sql(ddl).encode("utf-8")).hexdigest()
        if digest != item["normalized_sha256"] or digest != expected_hash:
            raise UpdateError(f"profile SQL hash differs for {name}")
    if profile["install_order"] != [
        "agent_jobs",
        "agent_job_items",
        "idx_agent_jobs_status",
        "idx_agent_job_items_status",
    ]:
        raise UpdateError("compatibility profile install order is invalid")
    if profile["remove_order"] != [
        "idx_agent_job_items_status",
        "idx_agent_jobs_status",
        "agent_job_items",
        "agent_jobs",
    ]:
        raise UpdateError("compatibility profile removal order is invalid")
    expected_legacy_sources = [
        {
            "kind": "official_migration_create",
            "url": "https://github.com/openai/codex/blob/rust-v0.142.2/codex-rs/state/migrations/0014_agent_jobs.sql",
        },
        {
            "kind": "official_migration_alter",
            "url": "https://github.com/openai/codex/blob/rust-v0.142.2/codex-rs/state/migrations/0015_agent_jobs_max_runtime_seconds.sql",
        },
        {
            "kind": "official_migration_drop",
            "url": "https://github.com/openai/codex/commit/687f05cb946d10c96f90dd7ce82e11465c6e20a7",
        },
        {
            "kind": "official_delete_implementation",
            "url": (
                "https://github.com/openai/codex/blob/rust-v0.142.2/"
                "codex-rs/state/src/runtime/threads.rs#L967-L1072"
            ),
        },
    ]
    if profile["sources"] != expected_legacy_sources:
        raise UpdateError("legacy compatibility sources differ from reviewed official links")
    return {
        "tail_chain_ids": [migration["tail_chain_id"]],
        "delete_contract_ids": [profile["delete_contract_id"]],
        "review_after": document["review_after"],
    }


def validate_tail_certificate_file(path: Path) -> dict[str, Any]:
    document = load_static_json(path, "tail certificate")
    require_exact_keys(
        document,
        {"schema_version", "updated_at", "review_after", "chains"},
        "tail certificate",
    )
    if document["schema_version"] != 1 or not isinstance(document["chains"], list):
        raise UpdateError("tail certificate has an unsupported shape")
    dt.date.fromisoformat(document["updated_at"])
    dt.date.fromisoformat(document["review_after"])
    if (
        document["updated_at"] != REVIEWED_UPDATED_AT
        or document["review_after"] != REVIEWED_REVIEW_AFTER
    ):
        raise UpdateError("tail certificate review dates are not the reviewed release")
    if len(document["chains"]) != 1:
        raise UpdateError("tail certificate must contain exactly one reviewed chain")
    chain = require_exact_keys(
        document["chains"][0],
        {
            "id",
            "delete_contract_id",
            "base_through",
            "max_successful",
            "classification",
            "migrations",
        },
        "tail certificate chain",
    )
    if (
        chain["id"] != REVIEWED_TAIL_CHAIN_ID
        or chain["delete_contract_id"] != REVIEWED_DELETE_CONTRACT_ID
        or chain["base_through"] != 42
        or chain["max_successful"] != 44
        or chain["classification"] != "reviewed_delete_unrelated"
        or not isinstance(chain["migrations"], list)
        or not all(isinstance(item, dict) for item in chain["migrations"])
        or [item.get("version") for item in chain["migrations"]] != [43, 44]
    ):
        raise UpdateError("tail certificate is not the exact reviewed 43-to-44 chain")

    migration_keys = {
        "version",
        "description",
        "success",
        "checksum_hex",
        "source_commit",
        "source_url",
        "reviewed_effects",
        "delete_review",
    }
    for item in chain["migrations"]:
        item = require_exact_keys(item, migration_keys, "tail migration certificate")
        version = item["version"]
        expected = REVIEWED_TAIL_MIGRATIONS[version]
        source_commit = item["source_commit"]
        source_url = item["source_url"]
        source_match = re.fullmatch(
            r"https://github\.com/openai/codex/blob/([0-9a-f]{40})/.+",
            str(source_url),
        )
        if (
            item["success"] != 1
            or not re.fullmatch(r"[0-9a-f]{40}", str(source_commit))
            or source_match is None
            or source_match.group(1) != source_commit
            or item["description"] != expected["description"]
            or item["checksum_hex"] != expected["checksum_hex"]
            or source_commit != expected["source_commit"]
            or source_url != expected["source_url"]
            or item["reviewed_effects"] != expected["reviewed_effects"]
            or item["delete_review"] != expected["delete_review"]
        ):
            raise UpdateError(
                f"tail migration {version} lacks the exact immutable reviewed fingerprint"
            )
    return {
        "tail_chain_ids": [chain["id"]],
        "delete_contract_ids": [chain["delete_contract_id"]],
        "review_after": document["review_after"],
    }


def validate_compatibility_files(root: Path) -> None:
    profile = validate_profile_file(
        root / "references" / "subagent-delete-compatibility.json"
    )
    tail = validate_tail_certificate_file(
        root / "references" / "subagent-delete-tail-certificates.json"
    )
    if (
        profile["tail_chain_ids"] != tail["tail_chain_ids"]
        or profile["delete_contract_ids"] != tail["delete_contract_ids"]
        or profile["review_after"] != tail["review_after"]
    ):
        raise UpdateError("compatibility profile and tail certificate are not bound together")


def validate_skill_tree(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", skill_text, re.DOTALL)
    if not frontmatter or "name: codex-storage-cleanup" not in frontmatter.group(1):
        raise UpdateError("SKILL.md front matter is invalid")
    if "description:" not in frontmatter.group(1):
        raise UpdateError("SKILL.md description is missing")
    (root / "README.md").read_text(encoding="utf-8")
    validate_compatibility_files(root)
    checks.append({"name": "skill-profile-and-tail-certificate", "ok": True})

    python_files = sorted((root / "scripts").glob("*.py"))
    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", *[str(path) for path in python_files]],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if compile_result.returncode != 0:
        raise UpdateError(f"Python compile validation failed: {compile_result.stderr.strip()}")
    checks.append({"name": "python-compile", "ok": True, "files": len(python_files)})

    test_files = sorted((root / "tests").glob("test_*.py"))
    if not test_files:
        raise UpdateError("trusted update contains no unit-test fixtures")
    checks.append(
        {
            "name": "tests-present-not-executed",
            "ok": True,
            "files": len(test_files),
            "reason": "automatic updater never executes newly fetched code with user credentials",
        }
    )

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell:
        command = (
            "$errorsFound = @(); "
            "Get-ChildItem -LiteralPath $env:CODEX_STORAGE_VALIDATE_ROOT -Recurse -Filter *.ps1 | "
            "ForEach-Object { $tokens=$null; $errors=$null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName, "
            "[ref]$tokens, [ref]$errors); if($errors){$errorsFound += $errors} }; "
            "if($errorsFound.Count -gt 0){$errorsFound | ForEach-Object {$_.Message}; exit 1}"
        )
        env = os.environ.copy()
        env["CODEX_STORAGE_VALIDATE_ROOT"] = str(root)
        ps_result = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if ps_result.returncode != 0:
            raise UpdateError(f"PowerShell parse validation failed: {ps_result.stdout.strip()}")
        checks.append({"name": "powershell-parse", "ok": True})
    else:
        raise UpdateError("PowerShell is unavailable for static syntax validation")
    return {"ok": True, "checks": checks}


def inspect_update(
    repo: Path = REPO_ROOT,
    *,
    incident_evidence: Path | None = None,
    compat_diagnosis: Path | None = None,
) -> dict[str, Any]:
    local = inspect_local_repo(repo)
    blockers = local_update_blockers(local)
    blockers.extend(incident_blockers(incident_evidence))
    diagnosis = compatibility_diagnosis(compat_diagnosis)
    if diagnosis and diagnosis["decision"] in {
        "known_workaround_eligible",
        "compat_installed",
        "unsafe_stop",
    }:
        blockers.append(
            f"compatibility diagnosis {diagnosis['decision']} requires incident handling, not source refresh"
        )
    profile_update_required = bool(
        diagnosis
        and diagnosis["decision"]
        in {"stale_profile_update_required", "unsupported_update_required"}
    )
    lifecycle = profile_lifecycle(repo)
    incident = None
    if incident_evidence is not None:
        resolved_incident = incident_evidence.resolve(strict=True)
        incident = {
            "path": str(resolved_incident),
            "sha256": hashlib.sha256(resolved_incident.read_bytes()).hexdigest(),
        }
    remote_sha: str | None = None
    relation: str | None = None
    network_error: str | None = None
    if local["origin_normalized"].casefold() == EXPECTED_REMOTE.casefold():
        try:
            remote_sha = remote_main_sha(repo)
            relation = remote_relation(repo, local["head"], remote_sha)
        except UpdateError as exc:
            network_error = str(exc)
            blockers.append("trusted remote could not be checked")
    else:
        blockers.append("remote lookup skipped because origin is not allow-listed")

    if blockers:
        decision = "automatic_fast_forward_blocked"
    elif relation == "up_to_date" and (lifecycle["stale"] or profile_update_required):
        decision = "draft_pr_required"
    elif relation == "up_to_date":
        decision = "up_to_date"
    elif relation in {"update_available_unfetched", "fast_forward_available"}:
        decision = "trusted_fast_forward_available"
    else:
        decision = "automatic_fast_forward_blocked"
    return {
        "schema_version": 1,
        "operation": "check-skill-update",
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "local": local,
        "profile_lifecycle": lifecycle,
        "compatibility_diagnosis": diagnosis,
        "incident_evidence": incident,
        "remote_main_sha": remote_sha,
        "relation": relation,
        "network_error": network_error,
        "blockers": sorted(set(blockers)),
        "decision": decision,
        "guarantees": [
            "no Codex database was opened",
            "no delete API was invoked",
            "the Codex CLI was not upgraded",
            "no branch was pushed or merged remotely",
        ],
    }


def safe_output(path: Path | None, repo: Path) -> Path | None:
    if path is None:
        return None
    parent = path.parent.resolve(strict=True)
    output = parent / path.name
    try:
        if os.path.commonpath(
            [os.path.normcase(str(output)), os.path.normcase(str(repo.resolve()))]
        ) == os.path.normcase(str(repo.resolve())):
            raise UpdateError("Update reports must be written outside the skill repository")
    except ValueError:
        pass
    if output.exists():
        raise UpdateError(f"Refusing to overwrite update report: {output}")
    return output


def emit(value: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    print(payload, end="")


def validate_remote_worktree(repo: Path) -> dict[str, Any]:
    temp_parent = Path(tempfile.gettempdir()).resolve()
    worktree = Path(tempfile.mkdtemp(prefix="codex-storage-skill-validate-", dir=temp_parent))
    # git worktree requires the destination not to exist.
    worktree.rmdir()
    added = False
    with tempfile.TemporaryDirectory(prefix="codex-storage-empty-hooks-") as hooks:
        hook_config = f"core.hooksPath={hooks}"
        try:
            run_git(
                ["-c", hook_config, "worktree", "add", "--detach", str(worktree), "origin/main"],
                cwd=repo,
            )
            added = True
            validation = validate_skill_tree(worktree)
            validation["commit"] = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
            return validation
        finally:
            if added:
                removal = run_git(
                    ["-c", hook_config, "worktree", "remove", "--force", str(worktree)],
                    cwd=repo,
                    check=False,
                )
                if removal.returncode != 0:
                    raise UpdateError(
                        "Validated worktree could not be removed safely: "
                        + (removal.stderr or removal.stdout).strip()
                    )
            elif worktree.exists():
                try:
                    worktree.rmdir()
                except OSError:
                    pass


def apply_update(args: argparse.Namespace, repo: Path = REPO_ROOT) -> dict[str, Any]:
    if not args.incident_evidence and not args.confirm_no_active_cleanup:
        raise UpdateError(
            "Apply requires --incident-evidence or --confirm-no-active-cleanup"
        )
    before = inspect_update(
        repo,
        incident_evidence=Path(args.incident_evidence) if args.incident_evidence else None,
        compat_diagnosis=Path(args.compat_diagnosis) if args.compat_diagnosis else None,
    )
    if before["blockers"]:
        raise UpdateError("; ".join(before["blockers"]))
    if not args.execute or args.confirm_token != UPDATE_TOKEN:
        raise UpdateError(
            f"Apply requires --execute --confirm-token {UPDATE_TOKEN} after reviewing check output"
        )
    if before["decision"] == "up_to_date":
        return {
            **before,
            "operation": "apply-skill-update",
            "status": "no-update-needed",
        }
    if before["decision"] == "draft_pr_required":
        raise UpdateError(
            "origin/main has no newer reviewed profile; collect evidence and create a tested Draft PR"
        )
    if before["decision"] != "trusted_fast_forward_available":
        raise UpdateError(f"Update is not a trusted fast-forward: {before['decision']}")

    run_git(["fetch", "origin", "main"], cwd=repo, timeout=120)
    fetched_sha = run_git(["rev-parse", "origin/main"], cwd=repo).stdout.strip()
    if fetched_sha != before["remote_main_sha"]:
        raise UpdateError("origin/main changed between check and fetch; rerun the update check")
    ancestor = run_git(
        ["merge-base", "--is-ancestor", before["local"]["head"], "origin/main"],
        cwd=repo,
        check=False,
    )
    if ancestor.returncode != 0:
        raise UpdateError("origin/main is not a descendant of local HEAD")
    validation = validate_remote_worktree(repo)
    if validation.get("commit") != fetched_sha:
        raise UpdateError("validated worktree commit differs from fetched origin/main")
    frozen = inspect_update(
        repo,
        incident_evidence=Path(args.incident_evidence) if args.incident_evidence else None,
        compat_diagnosis=Path(args.compat_diagnosis) if args.compat_diagnosis else None,
    )
    if frozen["blockers"]:
        raise UpdateError("repository or incident state changed: " + "; ".join(frozen["blockers"]))
    if frozen["local"]["head"] != before["local"]["head"]:
        raise UpdateError("local HEAD changed during update validation")
    if frozen["remote_main_sha"] != fetched_sha:
        raise UpdateError("origin/main changed during update validation; rerun the check")
    local_remote_ref = run_git(["rev-parse", "origin/main"], cwd=repo).stdout.strip()
    if local_remote_ref != fetched_sha:
        raise UpdateError("local origin/main moved after validation")
    if frozen["decision"] != "trusted_fast_forward_available":
        raise UpdateError(f"update decision changed during validation: {frozen['decision']}")
    with tempfile.TemporaryDirectory(prefix="codex-storage-empty-hooks-") as hooks:
        run_git(
            ["-c", f"core.hooksPath={hooks}", "merge", "--ff-only", fetched_sha],
            cwd=repo,
        )
    merged_head = run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if merged_head != fetched_sha:
        raise UpdateError("fast-forward completed at an unexpected commit")
    after_validation = validate_skill_tree(repo)
    after_lifecycle = profile_lifecycle(repo)
    return {
        "schema_version": 1,
        "operation": "apply-skill-update",
        "status": "fast-forwarded",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "before_commit": before["local"]["head"],
        "after_commit": run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip(),
        "remote_validation": validation,
        "local_validation": after_validation,
        "profile_lifecycle": after_lifecycle,
        "next_action": (
            "Run a new read-only compatibility diagnosis. Do not automatically retry a failed "
            "canary or resume deletion."
        ),
        "guarantees": before["guarantees"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check or apply a trusted fast-forward update to codex-storage-cleanup."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="Read-only remote update check")
    check_parser.add_argument("--incident-evidence")
    check_parser.add_argument("--compat-diagnosis")
    check_parser.add_argument("--output")
    apply_parser = subparsers.add_parser("apply", help="Validate and fast-forward local main")
    incident_group = apply_parser.add_mutually_exclusive_group(required=True)
    incident_group.add_argument("--incident-evidence")
    incident_group.add_argument("--confirm-no-active-cleanup", action="store_true")
    apply_parser.add_argument("--compat-diagnosis")
    apply_parser.add_argument("--execute", action="store_true")
    apply_parser.add_argument("--confirm-token")
    apply_parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    initial_head: str | None = None
    if args.command == "apply":
        try:
            initial_head = run_git(["rev-parse", "HEAD"], cwd=REPO_ROOT).stdout.strip()
        except UpdateError:
            initial_head = None
    try:
        output = safe_output(Path(args.output), REPO_ROOT) if args.output else None
        if args.command == "check":
            result = inspect_update(
                REPO_ROOT,
                incident_evidence=Path(args.incident_evidence)
                if args.incident_evidence
                else None,
                compat_diagnosis=Path(args.compat_diagnosis)
                if args.compat_diagnosis
                else None,
            )
            emit(result, output)
            return 0 if result["decision"] in {"up_to_date", "trusted_fast_forward_available"} else 3
        result = apply_update(args)
        emit(result, output)
        return 0
    except (UpdateError, OSError, ValueError, json.JSONDecodeError) as exc:
        current_head: str | None = None
        try:
            current_head = run_git(["rev-parse", "HEAD"], cwd=REPO_ROOT).stdout.strip()
        except UpdateError:
            current_head = None
        error = {
            "schema_version": 1,
            "operation": getattr(args, "command", "unknown"),
            "status": "stopped",
            "error": str(exc),
            "codex_database_opened": False,
            "delete_invoked": False,
            "codex_cli_updated": False,
            "remote_repository_modified": False,
            "local_repository_modified": (
                None
                if initial_head is None or current_head is None
                else initial_head != current_head
            ),
            "initial_head": initial_head,
            "current_head": current_head,
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
