from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "subagent_delete_compat.py"
SPEC = importlib.util.spec_from_file_location("subagent_delete_compat", SCRIPT)
assert SPEC and SPEC.loader
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


MIGRATIONS = [
    (
        14,
        "agent jobs",
        1,
        "12275BDF6BD1685525DBB54C37DDF62608D10F9F66110D7BB0DE55EE25A8B283E46D97CB8EABED7564F9752E3967BA8A",
    ),
    (
        15,
        "agent jobs max runtime seconds",
        1,
        "8104857BCB63E9665C77DDCB8186BE1BB630D9472FE215A0FADD62566FE33ABDA9CB223602506F2187F2BDD09D01105E",
    ),
    (
        42,
        "drop agent jobs",
        1,
        "815A1F0CBE21AC7F0653FB67C8E9702FD0EBB5F0A54CE644893B66D18614D9C8988510458B68FBD671B8632EF363B36A",
    ),
    (
        43,
        "threads is pinned",
        1,
        "9B2A199A557F5A92B0E27574E8BFC01DCE5EF28F106A3C16DFB3B8A6CD5679F3B8BC26B63E09C461A9A0C1364F0B04BF",
    ),
    (
        44,
        "external agent config imports provider id",
        1,
        "DFA22384943E691A089B9E8A6A8DA988EF1E25FF51DB7975CA42C3EC4BE474370F262CAA77AEB5877EEEABBFF2C3CEB1",
    ),
]


def version_result(version: str = "0.142.2") -> dict[str, object]:
    return {
        "ok": True,
        "version": version,
        "raw": f"codex-cli {version}",
        "error": None,
    }


class CompatibilityFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="compat-test-")
        self.root = Path(self.temp.name)
        self.codex_home = self.root / "CodexHome"
        self.external = self.root / "external"
        self.backup_dir = self.external / "backups"
        self.codex_home.mkdir()
        self.backup_dir.mkdir(parents=True)
        self.profile = Path(compat.DEFAULT_PROFILE)
        self._create_state_database(self.codex_home / "state_5.sqlite")
        self._create_simple_database(self.codex_home / "goals_1.sqlite")
        self._create_simple_database(self.codex_home / "memories_1.sqlite")
        self.failure_path = self.external / "failure.json"
        self.summary_path = self.external / "backup-summary.json"
        self._write_failure()
        self._write_backups()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_state_database(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE _sqlx_migrations ("
                "version BIGINT PRIMARY KEY, description TEXT NOT NULL, "
                "installed_on TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "success BOOLEAN NOT NULL, checksum BLOB NOT NULL, execution_time BIGINT NOT NULL)"
            )
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE thread_spawn_edges ("
                "parent_thread_id TEXT NOT NULL, child_thread_id TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO threads (id) VALUES (?)",
                ("019fbdae-bb7c-76c2-85f8-7e6af7ccb5d9",),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id) VALUES (?, ?)",
                (
                    "019fac31-bba4-74c1-96db-e393d5777977",
                    "019fbdae-bb7c-76c2-85f8-7e6af7ccb5d9",
                ),
            )
            for version, description, success, checksum in MIGRATIONS:
                connection.execute(
                    "INSERT INTO _sqlx_migrations "
                    "(version, description, success, checksum, execution_time) VALUES (?, ?, ?, ?, 1)",
                    (version, description, success, bytes.fromhex(checksum)),
                )
            connection.commit()

    def _create_simple_database(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            connection.commit()

    def _write_failure(self, *, message: str | None = None) -> None:
        payload = {
            "schema_version": 1,
            "canary_thread_id": "019fbdae-bb7c-76c2-85f8-7e6af7ccb5d9",
            "planned_bytes": 355977,
            "codex_cli_version": "0.142.2",
            "official_cli_command": "codex delete --force 019fbdae-bb7c-76c2-85f8-7e6af7ccb5d9",
            "official_cli_result": "failed to delete session",
            "app_server_error": {
                "code": -32603,
                "message": message
                or "failed to delete app-server state: error returned from database: "
                "(code: 1) no such table: agent_jobs",
            },
            "partial_deletion": {
                "rollout_file_absent": True,
                "state_thread_row_present": True,
                "spawn_edge_present": True,
                "state_quick_check": "ok",
            },
            "schema_evidence": {
                "live_state_database": str(self.codex_home / "state_5.sqlite"),
                "live_agent_jobs_present": False,
            },
            "safety_state": {
                "bulk_delete_started": False,
                "other_authorized_roots_deleted": 0,
                "database_backups_created": True,
                "automatic_compatibility_workaround_applied": False,
            },
        }
        self.failure_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_backups(self, *, created_at: dt.datetime | None = None) -> None:
        rows = []
        for name in sorted(compat.REQUIRED_BACKUPS):
            source = self.codex_home / name
            backup = self.backup_dir / name
            shutil.copy2(source, backup)
            rows.append(
                {
                    "database": name,
                    "source": str(source),
                    "source_bytes": source.stat().st_size,
                    "source_quick_check": "ok",
                    "backup": str(backup),
                    "backup_bytes": backup.stat().st_size,
                    "backup_quick_check": "ok",
                    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                }
            )
        payload = {
            "schema_version": 1,
            "created_at": (created_at or compat.utc_now()).isoformat(),
            "codex_home": str(self.codex_home),
            "backup_dir": str(self.backup_dir),
            "databases": rows,
        }
        self.summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def diagnose(
        self,
        *,
        version: str = "0.142.2",
        failure: bool = True,
        backup: bool = True,
        now: dt.datetime | None = None,
    ) -> dict[str, object]:
        with mock.patch.object(compat, "read_codex_version", return_value=version_result(version)):
            return compat.diagnose(
                self.codex_home,
                self.profile,
                None,
                self.failure_path if failure else None,
                self.summary_path if backup else None,
                now=now,
            )

    def _install_args(self, output: Path) -> argparse.Namespace:
        return argparse.Namespace(
            codex_home=str(self.codex_home),
            profile_file=str(self.profile),
            codex_exe=None,
            failure_evidence=str(self.failure_path),
            backup_summary=str(self.summary_path),
            execute=True,
            confirm_token=compat.INSTALL_TOKEN,
            output=str(output),
        )

    def _remove_args(self, install_output: Path, output: Path) -> argparse.Namespace:
        return argparse.Namespace(
            codex_home=str(self.codex_home),
            profile_file=str(self.profile),
            install_result=str(install_output),
            execute=True,
            confirm_token=compat.REMOVE_TOKEN,
            output=str(output),
        )

    def _install_legacy(self, output: Path) -> dict[str, object]:
        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_live_codex_version",
                return_value=None,
            ),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
        ):
            return compat.install_compat(self._install_args(output))

    def _compat_object_names(self) -> set[str]:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('agent_jobs','agent_job_items','idx_agent_jobs_status',"
                    "'idx_agent_job_items_status')"
                )
            }

    def _native_runtime(self, version: str = "0.146.0-alpha.9.2") -> dict[str, object]:
        executable = self.codex_home / "plugins" / ".plugin-appserver" / "codex.exe"
        return {
            "captured_at": compat.iso_now(),
            "ok": True,
            "reason": None,
            "desktop_process": {"process_id": 1234},
            "bundled_backend": {"path": "C:/Program Files/WindowsApps/OpenAI.Codex/app/resources/codex.exe", "sha256": "a" * 64},
            "mirror": {
                "path": str(executable),
                "sha256": "a" * 64,
                "authenticode": {"status": "Valid", "subject": "OpenAI OpCo, LLC"},
            },
            "cli": version_result(version),
        }

    def _native_evidence(self) -> dict[str, object]:
        runtime = self._native_runtime()
        database = compat.inspect_preflight_database(
            self.codex_home / "state_5.sqlite",
            json.loads(self.profile.read_text(encoding="utf-8"))["native_delete"][
                "required_migrations"
            ],
        )
        return {
            "schema_version": 2,
            "operation": "preflight",
            "decision": "canary_required",
            "native_delete": True,
            "allow_expensive_inventory": True,
            "recommended_codex_exe": runtime["mirror"]["path"],
            "runtime": runtime,
            "database": database,
            "condition_key": "a" * 64,
        }

    def test_profile_document_validates(self) -> None:
        document = json.loads(self.profile.read_text(encoding="utf-8"))
        profiles = compat.validate_profile_document(document)
        self.assertEqual(len(profiles), 1)

    def test_codex_version_preserves_prerelease_suffix(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="codex-cli 0.146.0-alpha.9.2\n",
            stderr="",
        )
        with mock.patch.object(compat.subprocess, "run", return_value=completed):
            result = compat.read_codex_version("codex-test.exe")
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], "0.146.0-alpha.9.2")

    def test_prerelease_runtime_meets_native_minimum(self) -> None:
        self.assertTrue(compat.semver_at_least("0.146.0-alpha.9.2", "0.145.0"))
        self.assertFalse(compat.semver_at_least("0.145.0-alpha.9", "0.145.0"))
        self.assertFalse(compat.semver_at_least("0.144.99-alpha.1", "0.145.0"))

    def test_native_preflight_accepts_new_tail_owned_by_matched_runtime(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "INSERT INTO _sqlx_migrations "
                "(version, description, success, checksum, execution_time) "
                "VALUES (45, 'future runtime-owned migration', 1, X'45', 1)"
            )
            connection.commit()
        with mock.patch.object(
            compat, "attest_desktop_runtime", return_value=self._native_runtime()
        ):
            report = compat.preflight(self.codex_home, self.profile)
        self.assertEqual(report["decision"], "canary_required")
        self.assertTrue(report["native_delete"])
        self.assertTrue(report["allow_expensive_inventory"])
        self.assertEqual(report["database"]["max_successful_migration"], 45)

    def test_native_preflight_rejects_unpaired_runtime(self) -> None:
        runtime = self._native_runtime()
        runtime["ok"] = False
        runtime["reason"] = "desktop backend mirror hash differs"
        with mock.patch.object(compat, "attest_desktop_runtime", return_value=runtime):
            report = compat.preflight(self.codex_home, self.profile)
        self.assertEqual(report["decision"], "unsupported_update_required")
        self.assertFalse(report["allow_expensive_inventory"])
        self.assertTrue(any("hash differs" in reason for reason in report["reasons"]))

    def test_desktop_discovery_rejects_fake_root_and_multiple_processes(self) -> None:
        package = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "WindowsApps"
            / "OpenAI.Codex_fixture_x64__publisher"
        )
        valid = {
            "process_id": 10,
            "parent_process_id": 9,
            "executable_path": str(package / "app" / "resources" / "codex.exe"),
            "command_line": "codex.exe app-server",
            "parent_name": "ChatGPT.exe",
            "parent_executable_path": str(package / "app" / "ChatGPT.exe"),
        }
        fake = dict(valid)
        fake["executable_path"] = str(
            self.root / "Program Files" / "WindowsApps" / "OpenAI.Codex_fake" / "app" / "resources" / "codex.exe"
        )
        fake["parent_executable_path"] = str(self.root / "ChatGPT.exe")
        with mock.patch.object(compat, "run_powershell_json", return_value=[fake]):
            with self.assertRaises(compat.SafetyError):
                compat.discover_desktop_app_server()
        second = dict(valid, process_id=11)
        with mock.patch.object(
            compat, "run_powershell_json", return_value=[valid, second]
        ):
            with self.assertRaises(compat.SafetyError):
                compat.discover_desktop_app_server()

    def test_runtime_attestation_ignores_path_powershell(self) -> None:
        fake = self.root / "powershell.exe"
        fake.write_bytes(b"not a trusted shell")
        completed = mock.Mock(returncode=0, stdout="{}\n", stderr="")
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.root)}),
            mock.patch.object(
                compat.shutil,
                "which",
                side_effect=AssertionError("PATH lookup must not be used"),
            ),
            mock.patch.object(compat.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(compat.run_powershell_json("$null"), {})

        executable = Path(run.call_args.args[0][0])
        self.assertNotEqual(executable, fake)
        self.assertEqual(executable.name.casefold(), "powershell.exe")
        self.assertIn("windowspowershell", str(executable).casefold())

    def test_attestation_requires_exact_openai_publisher(self) -> None:
        bundled = self.root / "bundled-codex.exe"
        mirror = self.codex_home / "plugins" / ".plugin-appserver" / "codex.exe"
        mirror.parent.mkdir(parents=True)
        bundled.write_bytes(b"same signed fixture")
        mirror.write_bytes(b"same signed fixture")
        process = {
            "process_id": 10,
            "parent_process_id": 9,
            "executable_path": str(bundled),
            "command_line": "codex.exe app-server",
            "parent_name": "ChatGPT.exe",
            "parent_executable_path": str(self.root / "ChatGPT.exe"),
        }
        signature = {
            "status": "Valid",
            "subject": "CN=Attacker, OU=OpenAI OpCo, LLC wildcard lookalike",
            "publisher": "Attacker",
            "thumbprint": "A" * 40,
        }
        with (
            mock.patch.object(compat, "discover_desktop_app_server", return_value=process),
            mock.patch.object(compat, "authenticode_evidence", return_value=signature),
        ):
            evidence = compat.attest_desktop_runtime(self.codex_home)
        self.assertFalse(evidence["ok"])
        self.assertIn("valid OpenAI signature", str(evidence["reason"]))

    def test_native_diagnosis_requires_backup_and_binds_preflight_database(self) -> None:
        evidence = self._native_evidence()
        with (
            mock.patch.object(
                compat,
                "validate_runtime_evidence",
                return_value=(evidence, [], self.failure_path),
            ),
            mock.patch.object(
                compat, "attest_desktop_runtime", return_value=evidence["runtime"]
            ),
        ):
            missing = compat.diagnose(
                self.codex_home,
                self.profile,
                None,
                None,
                None,
                self.failure_path,
            )
        self.assertEqual(missing["decision"], "unsafe_stop")
        self.assertTrue(any("backup" in reason for reason in missing["reasons"]))

        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "INSERT INTO _sqlx_migrations "
                "(version, description, success, checksum, execution_time) "
                "VALUES (45, 'post-preflight drift', 1, X'45', 1)"
            )
            connection.commit()
        self._write_backups()
        with (
            mock.patch.object(
                compat,
                "validate_runtime_evidence",
                return_value=(evidence, [], self.failure_path),
            ),
            mock.patch.object(
                compat, "attest_desktop_runtime", return_value=evidence["runtime"]
            ),
        ):
            drifted = compat.diagnose(
                self.codex_home,
                self.profile,
                None,
                None,
                self.summary_path,
                self.failure_path,
            )
        self.assertEqual(drifted["decision"], "unsafe_stop")
        self.assertTrue(any("migration ledger changed" in reason for reason in drifted["reasons"]))

    def test_native_preflight_rejects_runtime_before_delete_fix(self) -> None:
        with mock.patch.object(
            compat,
            "attest_desktop_runtime",
            return_value=self._native_runtime("0.144.0"),
        ):
            report = compat.preflight(self.codex_home, self.profile)
        self.assertEqual(report["decision"], "unsupported_update_required")
        self.assertFalse(report["native_delete"])

    def test_native_preflight_rejects_temporary_compat_objects(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE agent_jobs (id TEXT PRIMARY KEY)")
            connection.commit()
        with mock.patch.object(
            compat, "attest_desktop_runtime", return_value=self._native_runtime()
        ):
            report = compat.preflight(self.codex_home, self.profile)
        self.assertEqual(report["decision"], "unsupported_update_required")
        self.assertFalse(report["allow_expensive_inventory"])

    def test_preflight_condition_key_is_stable_for_unchanged_state(self) -> None:
        runtime = self._native_runtime()
        with mock.patch.object(compat, "attest_desktop_runtime", return_value=runtime):
            first = compat.preflight(
                self.codex_home,
                self.profile,
                now=dt.datetime(2026, 8, 3, 1, tzinfo=dt.timezone.utc),
            )
            second = compat.preflight(
                self.codex_home,
                self.profile,
                now=dt.datetime(2026, 8, 3, 2, tzinfo=dt.timezone.utc),
            )
        self.assertEqual(first["condition_key"], second["condition_key"])

    def test_profile_rejects_self_hashed_but_unreviewed_ddl(self) -> None:
        document = json.loads(self.profile.read_text(encoding="utf-8"))
        profile = document["profiles"][0]
        for item in profile["objects"]:
            ddl = f"CREATE INDEX {item['name']} ON threads(id)"
            item["type"] = "index"
            item["ddl"] = ddl
            item["normalized_sha256"] = compat.sql_hash(ddl)
        with self.assertRaises(compat.SafetyError):
            compat.validate_profile_document(document)

    def test_legacy_cli_cannot_start_a_new_canary(self) -> None:
        report = self.diagnose(failure=False, backup=False)
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_exact_failure_and_fresh_backups_are_eligible(self) -> None:
        report = self.diagnose()
        self.assertEqual(report["decision"], "known_workaround_eligible")

    def test_cli_version_alone_never_matches_an_unknown_version(self) -> None:
        report = self.diagnose(version="0.143.0")
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_migration_checksum_mismatch_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "UPDATE _sqlx_migrations SET checksum = X'00' WHERE version = 42"
            )
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_reviewed_tail_43_44_exact_chain_is_accepted(self) -> None:
        report = self.diagnose(failure=False, backup=False)
        self.assertEqual(report["decision"], "unsupported_update_required")
        self.assertEqual(report["database"]["max_successful_migration"], 44)

    def test_reviewed_tail_wrong_checksum_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "UPDATE _sqlx_migrations SET checksum = X'00' WHERE version = 43"
            )
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_reviewed_tail_gap_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute("DELETE FROM _sqlx_migrations WHERE version = 43")
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_incomplete_reviewed_tail_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute("DELETE FROM _sqlx_migrations WHERE version = 44")
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_unknown_successful_tail_migration_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "INSERT INTO _sqlx_migrations "
                "(version, description, success, checksum, execution_time) "
                "VALUES (45, 'unknown future migration', 1, X'45', 1)"
            )
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_any_failed_migration_row_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "INSERT INTO _sqlx_migrations "
                "(version, description, success, checksum, execution_time) "
                "VALUES (45, 'failed future migration', 0, X'45', 1)"
            )
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsupported_update_required")

    def test_different_missing_table_error_does_not_reuse_old_fix(self) -> None:
        self._write_failure(message="error returned from database: no such table: threads")
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsafe_stop")

    def test_expired_profile_requires_update(self) -> None:
        report = self.diagnose(
            now=dt.datetime(2026, 11, 2, tzinfo=dt.timezone.utc)
        )
        self.assertEqual(report["decision"], "stale_profile_update_required")

    def test_partial_compatibility_objects_stop(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE agent_jobs (id TEXT PRIMARY KEY)")
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsafe_stop")

    def test_stale_backup_stops(self) -> None:
        self._write_backups(
            created_at=compat.utc_now() - dt.timedelta(hours=3)
        )
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsafe_stop")
        self.assertTrue(any("stale" in item for item in report["reasons"]))

    def test_backup_hardlink_to_live_database_stops(self) -> None:
        backup = self.backup_dir / "state_5.sqlite"
        backup.unlink()
        os.link(self.codex_home / "state_5.sqlite", backup)

        report = self.diagnose()

        self.assertEqual(report["decision"], "unsafe_stop")
        self.assertTrue(any("hardlink" in item for item in report["reasons"]))

    def test_stale_failure_evidence_stops(self) -> None:
        old_time = (compat.utc_now() - dt.timedelta(hours=30)).timestamp()
        os.utime(self.failure_path, (old_time, old_time))
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsafe_stop")
        self.assertTrue(any("failure evidence is stale" in item for item in report["reasons"]))

    def test_live_canary_state_drift_stops(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "DELETE FROM thread_spawn_edges WHERE child_thread_id = ?",
                ("019fbdae-bb7c-76c2-85f8-7e6af7ccb5d9",),
            )
            connection.commit()
        report = self.diagnose()
        self.assertEqual(report["decision"], "unsafe_stop")
        self.assertTrue(any("live canary" in item for item in report["reasons"]))

    def test_execute_rejects_external_profile_copy(self) -> None:
        copied_profile = self.external / "copied-profile.json"
        shutil.copy2(self.profile, copied_profile)
        install_args = argparse.Namespace(
            codex_home=str(self.codex_home),
            profile_file=str(copied_profile),
            codex_exe=None,
            failure_evidence=str(self.failure_path),
            backup_summary=str(self.summary_path),
            execute=True,
            confirm_token=compat.INSTALL_TOKEN,
            output=str(self.external / "external-profile-result.json"),
        )
        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            self.assertRaises(compat.SafetyError),
        ):
            compat.install_compat(install_args)
        inspection = self.diagnose(failure=False, backup=False)
        self.assertEqual(inspection["decision"], "unsupported_update_required")

    def test_post_commit_output_failure_keeps_commit_pending_journal(self) -> None:
        install_output = self.external / "install-write-failure.json"
        install_args = argparse.Namespace(
            codex_home=str(self.codex_home),
            profile_file=str(self.profile),
            codex_exe=None,
            failure_evidence=str(self.failure_path),
            backup_summary=str(self.summary_path),
            execute=True,
            confirm_token=compat.INSTALL_TOKEN,
            output=str(install_output),
        )
        real_write = compat.write_json_atomic
        writes = 0

        def fail_final_result(path: Path, payload: dict[str, object]) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full")
            real_write(path, payload)

        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            mock.patch.object(compat, "write_json_atomic", side_effect=fail_final_result),
            self.assertRaises(OSError),
        ):
            compat.install_compat(install_args)
        journal = json.loads(install_output.read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "commit-pending")
        self.assertRegex(journal["run_nonce"], r"^[0-9a-f]{32}$")
        self.assertGreater(journal["installed_schema_version"], 0)
        self.assertEqual(
            set(journal["created_object_rootpages"]), compat.KNOWN_OBJECTS
        )
        self.assertTrue(
            all(value > 0 for value in journal["created_object_rootpages"].values())
        )
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('agent_jobs','agent_job_items','idx_agent_jobs_status',"
                    "'idx_agent_job_items_status')"
                )
            }
            self.assertEqual(present, compat.KNOWN_OBJECTS)
            for name in (
                "idx_agent_job_items_status",
                "idx_agent_jobs_status",
                "agent_job_items",
                "agent_jobs",
            ):
                connection.execute(compat.DROP_STATEMENTS[name])
            connection.commit()

    def test_install_stops_if_full_migration_ledger_drifts_after_diagnosis(self) -> None:
        install_output = self.external / "install-ledger-drift.json"

        def drift_ledger(*_args: object, **_kwargs: object) -> None:
            with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
                connection.execute(
                    "UPDATE _sqlx_migrations SET execution_time = execution_time + 1 "
                    "WHERE version = 43"
                )
                connection.commit()

        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_live_codex_version",
                side_effect=drift_ledger,
            ),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            self.assertRaisesRegex(compat.SafetyError, "migration|ledger|history"),
        ):
            compat.install_compat(self._install_args(install_output))
        self.assertEqual(self._compat_object_names(), set())

    def test_install_stops_if_base_schema_drifts_after_diagnosis(self) -> None:
        install_output = self.external / "install-schema-drift.json"

        def drift_schema(*_args: object, **_kwargs: object) -> None:
            with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
                connection.execute("CREATE TABLE unrelated_schema_drift (id INTEGER)")
                connection.commit()

        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_live_codex_version",
                side_effect=drift_schema,
            ),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            self.assertRaisesRegex(compat.SafetyError, "schema"),
        ):
            compat.install_compat(self._install_args(install_output))
        self.assertEqual(self._compat_object_names(), set())

    def test_install_stops_if_schema_cookie_drifts_after_diagnosis(self) -> None:
        install_output = self.external / "install-schema-cookie-drift.json"

        def drift_schema_cookie(*_args: object, **_kwargs: object) -> None:
            with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
                connection.execute("PRAGMA schema_version = 103")
                connection.commit()

        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_live_codex_version",
                side_effect=drift_schema_cookie,
            ),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            self.assertRaisesRegex(compat.SafetyError, "schema_version"),
        ):
            compat.install_compat(self._install_args(install_output))
        self.assertEqual(self._compat_object_names(), set())

    def test_remove_stops_if_full_migration_ledger_drifts_after_install(self) -> None:
        install_output = self.external / "install-before-ledger-drift.json"
        installed = self._install_legacy(install_output)
        self.assertIn("migration_history_sha256", installed)
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "UPDATE _sqlx_migrations SET execution_time = execution_time + 1 "
                "WHERE version = 43"
            )
            connection.commit()

        removal_args = self._remove_args(
            install_output, self.external / "remove-ledger-drift.json"
        )
        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(compat, "require_live_codex_version", return_value=None),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            self.assertRaisesRegex(compat.SafetyError, "migration|ledger|history"),
        ):
            compat.remove_compat(removal_args)
        self.assertEqual(self._compat_object_names(), compat.KNOWN_OBJECTS)

    def test_remove_stops_if_base_schema_drifts_after_install(self) -> None:
        install_output = self.external / "install-before-schema-drift.json"
        installed = self._install_legacy(install_output)
        self.assertIn("base_schema_sha256", installed)
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute("ALTER TABLE threads ADD COLUMN unrelated_drift TEXT")
            connection.commit()

        removal_args = self._remove_args(
            install_output, self.external / "remove-schema-drift.json"
        )
        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(compat, "require_live_codex_version", return_value=None),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            self.assertRaisesRegex(compat.SafetyError, "schema"),
        ):
            compat.remove_compat(removal_args)
        self.assertEqual(self._compat_object_names(), compat.KNOWN_OBJECTS)

    def test_remove_stops_if_bound_evidence_changes_after_prepare(self) -> None:
        install_output = self.external / "install-before-evidence-drift.json"
        self._install_legacy(install_output)
        removal_args = self._remove_args(
            install_output, self.external / "remove-evidence-drift.json"
        )
        real_write = compat.write_json_exclusive

        def mutate_after_prepare(path: Path, payload: dict[str, object]) -> None:
            real_write(path, payload)
            self.failure_path.write_text(
                self.failure_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(compat, "require_live_codex_version", return_value=None),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
            mock.patch.object(
                compat,
                "write_json_exclusive",
                side_effect=mutate_after_prepare,
            ),
            self.assertRaisesRegex(compat.SafetyError, "failure evidence changed"),
        ):
            compat.remove_compat(removal_args)
        self.assertEqual(self._compat_object_names(), compat.KNOWN_OBJECTS)

    def test_install_and_remove_exact_objects(self) -> None:
        install_output = self.external / "install-result.json"
        install_args = argparse.Namespace(
            codex_home=str(self.codex_home),
            profile_file=str(self.profile),
            codex_exe=None,
            failure_evidence=str(self.failure_path),
            backup_summary=str(self.summary_path),
            execute=True,
            confirm_token=compat.INSTALL_TOKEN,
            output=str(install_output),
        )
        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
        ):
            installed = compat.install_compat(install_args)
        self.assertEqual(installed["status"], "installed")
        self.assertGreater(installed["installed_schema_version"], 0)
        self.assertRegex(installed["migration_history_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(installed["base_schema_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(installed["created_object_rootpages"]), compat.KNOWN_OBJECTS
        )
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'agent_job%' "
                    "OR name LIKE 'idx_agent_job%'"
                )
            }
        self.assertEqual(present, compat.KNOWN_OBJECTS)

        removal_output = self.external / "remove-result.json"
        removal_args = argparse.Namespace(
            codex_home=str(self.codex_home),
            profile_file=str(self.profile),
            install_result=str(install_output),
            execute=True,
            confirm_token=compat.REMOVE_TOKEN,
            output=str(removal_output),
        )
        with (
            mock.patch.object(compat, "read_codex_version", return_value=version_result()),
            mock.patch.object(
                compat,
                "require_repository_profile",
                side_effect=lambda path: Path(path),
            ),
        ):
            removed = compat.remove_compat(removal_args)
        self.assertEqual(removed["status"], "removed")
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            present_after = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
                "('agent_jobs','agent_job_items','idx_agent_jobs_status',"
                "'idx_agent_job_items_status')"
            ).fetchone()[0]
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            migration_max = connection.execute(
                "SELECT MAX(version) FROM _sqlx_migrations WHERE success = 1"
            ).fetchone()[0]
        self.assertEqual(present_after, 0)
        self.assertEqual(quick, "ok")
        self.assertEqual(migration_max, 44)


if __name__ == "__main__":
    unittest.main()
