from __future__ import annotations

import datetime as dt
import argparse
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh_skill.py"
SPEC = importlib.util.spec_from_file_location("refresh_skill", SCRIPT)
assert SPEC and SPEC.loader
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


class RefreshSkillTests(unittest.TestCase):
    def test_remote_normalization_accepts_only_canonical_repository_shapes(self) -> None:
        expected = "https://github.com/skills-qweer/codex-storage-cleanup"
        self.assertEqual(
            refresh.normalize_remote(
                "https://github.com/skills-qweer/codex-storage-cleanup.git"
            ),
            expected,
        )
        self.assertEqual(
            refresh.normalize_remote("git@github.com:skills-qweer/codex-storage-cleanup.git"),
            expected,
        )
        self.assertNotEqual(
            refresh.normalize_remote("https://example.com/codex-storage-cleanup.git"),
            expected,
        )

    def test_dirty_or_wrong_branch_blocks_fast_forward(self) -> None:
        state = {
            "origin_normalized": refresh.EXPECTED_REMOTE,
            "origin_raw": refresh.EXPECTED_REMOTE + ".git",
            "branch": "feature/test",
            "upstream": "origin/main",
            "dirty_entries": ["?? local.txt"],
            "operation_markers": [],
        }
        blockers = refresh.local_update_blockers(state)
        self.assertTrue(any("branch" in item for item in blockers))
        self.assertTrue(any("working tree" in item for item in blockers))

    def test_partial_deletion_evidence_blocks_source_refresh(self) -> None:
        with tempfile.TemporaryDirectory(prefix="incident-test-") as temp:
            path = Path(temp) / "incident.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
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
                ),
                encoding="utf-8",
            )
            blockers = refresh.incident_blockers(path)
        self.assertTrue(any("partial deletion" in item for item in blockers))

    def test_unknown_incident_evidence_blocks_source_refresh(self) -> None:
        with tempfile.TemporaryDirectory(prefix="incident-test-") as temp:
            path = Path(temp) / "incident.json"
            path.write_text("{}", encoding="utf-8")
            blockers = refresh.incident_blockers(path)
        self.assertTrue(any("unknown" in item for item in blockers))

    def test_unsupported_diagnosis_requires_draft_pr_when_remote_is_current(self) -> None:
        local = {
            "origin_normalized": refresh.EXPECTED_REMOTE,
            "origin_raw": refresh.EXPECTED_REMOTE + ".git",
            "branch": "main",
            "upstream": "origin/main",
            "head": "a" * 40,
            "dirty_entries": [],
            "operation_markers": [],
            "tls_verification_disabled": False,
        }
        diagnosis = {
            "path": "diagnosis.json",
            "sha256": "b" * 64,
            "decision": "unsupported_update_required",
            "profile_sha256": "c" * 64,
        }
        with (
            mock.patch.object(refresh, "inspect_local_repo", return_value=local),
            mock.patch.object(refresh, "incident_blockers", return_value=[]),
            mock.patch.object(refresh, "compatibility_diagnosis", return_value=diagnosis),
            mock.patch.object(
                refresh,
                "profile_lifecycle",
                return_value={"path": "profile.json", "review_after": "2026-11-01", "stale": False},
            ),
            mock.patch.object(refresh, "remote_main_sha", return_value="a" * 40),
            mock.patch.object(refresh, "remote_relation", return_value="up_to_date"),
        ):
            result = refresh.inspect_update(Path("."), compat_diagnosis=Path("diagnosis.json"))
        self.assertEqual(result["decision"], "draft_pr_required")

    def test_profile_lifecycle_marks_expired_matrix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-test-") as temp:
            root = Path(temp)
            references = root / "references"
            references.mkdir()
            (references / "subagent-delete-compatibility.json").write_text(
                json.dumps(
                    {
                        "review_after": "2026-11-01",
                        "profiles": [{"review_after": "2026-10-01"}],
                    }
                ),
                encoding="utf-8",
            )
            result = refresh.profile_lifecycle(root, today=dt.date(2026, 10, 2))
        self.assertTrue(result["stale"])
        self.assertEqual(result["review_after"], "2026-10-01")

    def test_current_profile_hashes_validate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        refresh.validate_compatibility_files(root)

    def test_static_validator_rejects_weakened_native_runtime_controls(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "subagent-delete-compatibility.json"
        )
        for field, value in (
            ("requires_desktop_runtime_hash_match", False),
            ("requires_valid_openai_signature", False),
            ("official_canary_required", False),
            ("workaround_allowed", True),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory(
                prefix="profile-test-"
            ) as temp:
                profile = Path(temp) / "profile.json"
                document = json.loads(source.read_text(encoding="utf-8"))
                document["native_delete"][field] = value
                profile.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(refresh.UpdateError):
                    refresh.validate_profile_file(profile)

    def test_static_validator_rejects_self_extended_review_dates(self) -> None:
        source = Path(__file__).resolve().parents[1] / "references"
        with tempfile.TemporaryDirectory(prefix="review-date-test-") as temp:
            root = Path(temp)
            references = root / "references"
            shutil.copytree(source, references)
            profile_path = references / "subagent-delete-compatibility.json"
            tail_path = references / "subagent-delete-tail-certificates.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            tail = json.loads(tail_path.read_text(encoding="utf-8"))
            profile["review_after"] = "2099-12-31"
            profile["profiles"][0]["review_after"] = "2099-12-31"
            tail["review_after"] = "2099-12-31"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            tail_path.write_text(json.dumps(tail), encoding="utf-8")
            with self.assertRaises(refresh.UpdateError):
                refresh.validate_compatibility_files(root)

    def test_incident_deleted_root_count_must_be_nonnegative_integer(self) -> None:
        base = {
            "schema_version": 1,
            "partial_deletion": {
                "rollout_file_absent": False,
                "state_thread_row_present": False,
                "spawn_edge_present": False,
                "state_quick_check": "ok",
            },
            "safety_state": {
                "bulk_delete_started": False,
                "other_authorized_roots_deleted": 0,
                "database_backups_created": True,
                "automatic_compatibility_workaround_applied": False,
            },
        }
        for invalid in (-1, 0.5, False):
            with self.subTest(value=invalid), tempfile.TemporaryDirectory(
                prefix="incident-count-test-"
            ) as temp:
                path = Path(temp) / "incident.json"
                payload = json.loads(json.dumps(base))
                payload["safety_state"]["other_authorized_roots_deleted"] = invalid
                path.write_text(json.dumps(payload), encoding="utf-8")
                blockers = refresh.incident_blockers(path)
                self.assertTrue(any("count is invalid" in item for item in blockers))

    def test_static_validator_rejects_tail_range_wildcard_and_self_approval(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "subagent-delete-tail-certificates.json"
        )

        def add_range(document: dict[str, object]) -> None:
            document["chains"][0]["migration_range"] = "43-*"  # type: ignore[index]

        def add_wildcard(document: dict[str, object]) -> None:
            document["chains"][0]["migrations"][0]["source_url"] = (  # type: ignore[index]
                "https://github.com/openai/codex/blob/*/codex-rs/state/migrations/"
                "0043_threads_is_pinned.sql"
            )

        def add_self_approval(document: dict[str, object]) -> None:
            document["chains"][0]["migrations"][0]["self_approved"] = True  # type: ignore[index]

        for name, mutate in (
            ("range", add_range),
            ("wildcard", add_wildcard),
            ("self-approval", add_self_approval),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory(
                prefix="tail-test-"
            ) as temp:
                certificate = Path(temp) / "tail.json"
                document = json.loads(source.read_text(encoding="utf-8"))
                mutate(document)
                certificate.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(refresh.UpdateError):
                    refresh.validate_tail_certificate_file(certificate)

    def test_static_validator_rejects_self_declared_new_tail_migration(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "subagent-delete-tail-certificates.json"
        )
        with tempfile.TemporaryDirectory(prefix="tail-test-") as temp:
            certificate = Path(temp) / "tail.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            chain = document["chains"][0]
            invented = dict(chain["migrations"][-1])
            invented.update(
                {
                    "version": 45,
                    "description": "declared safe by this file",
                    "source_commit": "a" * 40,
                    "source_url": (
                        "https://github.com/openai/codex/blob/"
                        + "a" * 40
                        + "/codex-rs/state/migrations/0045_declared_safe.sql"
                    ),
                }
            )
            chain["migrations"].append(invented)
            chain["max_successful"] = 45
            certificate.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(refresh.UpdateError):
                refresh.validate_tail_certificate_file(certificate)

    def test_static_validator_rejects_self_hashed_unreviewed_ddl(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "subagent-delete-compatibility.json"
        )
        with tempfile.TemporaryDirectory(prefix="profile-test-") as temp:
            profile = Path(temp) / "profile.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            item = document["profiles"][0]["objects"][0]
            item["ddl"] = "CREATE INDEX agent_jobs ON threads(id)"
            item["type"] = "index"
            item["normalized_sha256"] = hashlib.sha256(
                refresh.normalize_sql(item["ddl"]).encode("utf-8")
            ).hexdigest()
            profile.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(refresh.UpdateError):
                refresh.validate_profile_file(profile)

    def test_trusted_fast_forward_in_temporary_repository(self) -> None:
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="refresh-integration-") as temp:
            root = Path(temp)
            local = root / "local"
            remote = root / "remote.git"
            upstream = root / "upstream"
            shutil.copytree(
                source,
                local,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            refresh.run_git(["init", "--bare", str(remote)], cwd=root)
            refresh.run_git(["init", "-b", "main"], cwd=local)
            refresh.run_git(["config", "user.name", "Updater Test"], cwd=local)
            refresh.run_git(["config", "user.email", "updater-test@example.invalid"], cwd=local)
            refresh.run_git(["add", "."], cwd=local)
            refresh.run_git(["commit", "-m", "baseline"], cwd=local)
            refresh.run_git(["remote", "add", "origin", str(remote)], cwd=local)
            refresh.run_git(["push", "-u", "origin", "main"], cwd=local)

            refresh.run_git(["clone", str(remote), str(upstream)], cwd=root)
            refresh.run_git(["config", "user.name", "Updater Test"], cwd=upstream)
            refresh.run_git(["config", "user.email", "updater-test@example.invalid"], cwd=upstream)
            readme = upstream / "README.md"
            readme.write_text(
                readme.read_text(encoding="utf-8") + "\nTrusted update fixture.\n",
                encoding="utf-8",
            )
            refresh.run_git(["add", "README.md"], cwd=upstream)
            refresh.run_git(["commit", "-m", "trusted update"], cwd=upstream)
            refresh.run_git(["push", "origin", "main"], cwd=upstream)

            args = argparse.Namespace(
                incident_evidence=None,
                confirm_no_active_cleanup=True,
                compat_diagnosis=None,
                execute=True,
                confirm_token=refresh.UPDATE_TOKEN,
            )
            expected_remote = refresh.normalize_remote(str(remote))
            with mock.patch.object(refresh, "EXPECTED_REMOTE", expected_remote):
                result = refresh.apply_update(args, repo=local)
            self.assertEqual(result["status"], "fast-forwarded")
            self.assertEqual(
                refresh.run_git(["rev-parse", "HEAD"], cwd=local).stdout.strip(),
                refresh.run_git(["rev-parse", "refs/heads/main"], cwd=remote).stdout.strip(),
            )


if __name__ == "__main__":
    unittest.main()
