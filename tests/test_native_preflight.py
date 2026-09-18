from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "subagent_delete_compat.py"
SPEC = importlib.util.spec_from_file_location("native_compat", SCRIPT)
assert SPEC and SPEC.loader
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)

SIGNATURE = {"status": "Valid", "publisher": "OpenAI OpCo, LLC", "thumbprint": "B" * 40}


def request_schema() -> dict:
    def obj(properties: dict, required: list[str]) -> dict:
        return {"type": "object", "properties": properties, "required": required}

    text = {"type": "string"}
    return {
        "oneOf": [
            obj({"id": {"type": "integer"}, "method": {"const": method}, "params": {"$ref": f"#/definitions/{reference}"}}, ["id", "method", "params"])
            for method, reference in (("initialize", "Init"), ("thread/delete", "Delete"))
        ],
        "definitions": {
            "Init": obj({
                "clientInfo": obj({"name": text, "version": text}, ["name", "version"]),
                "capabilities": {"anyOf": [obj({"experimentalApi": {"type": "boolean"}}, []), {"type": "null"}]},
            }, ["clientInfo"]),
            "Delete": obj({"threadId": text}, ["threadId"]),
        },
    }


class NativePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="native-preflight-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.database = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                "CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, archived INTEGER);"
                "CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);"
                "CREATE TABLE _sqlx_migrations(version INTEGER, description TEXT, success INTEGER, checksum BLOB);"
                "INSERT INTO _sqlx_migrations VALUES(99, 'new schema without historical anchors', 1, X'99');"
            )
        self.executable = self.root / "arbitrary-layout" / "codex.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"official fixture")
        self.process = {
            "process_id": 1234, "parent_process_id": 1000,
            "created_at": "2026-09-19T00:00:00Z",
            "executable_path": str(self.executable),
            "parent_executable_path": str(self.root / "desktop.exe"),
            "parent_name": "desktop.exe", "command_line": "codex.exe app-server",
        }
        metadata = self.executable.stat()
        self.runtime = {
            "ok": True, "reason": None, "desktop_process": self.process,
            "executable": {"path": str(self.executable), "sha256": "a" * 64, "bytes": metadata.st_size, "mtime_ns": metadata.st_mtime_ns},
            "cli": {"ok": True, "version": "99.1.0-beta.99"},
            "capabilities": {"ok": True, "methods": ["initialize", "thread/delete"], "problems": []},
        }

    def preflight(self, **kwargs: object) -> dict:
        with mock.patch.object(compat, "attest_desktop_runtime", return_value=self.runtime):
            return compat.preflight(self.home, self.root / "absent-legacy-profile.json", **kwargs)

    def test_future_version_and_date_do_not_need_a_profile_update(self) -> None:
        report = self.preflight(now=dt.datetime(2035, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(report["decision"], "canary_required")
        self.assertEqual(report["recommended_codex_exe"], str(self.executable))

    def test_new_version_label_uses_capabilities(self) -> None:
        self.runtime["cli"] = {"ok": False, "version": None, "raw": "codex next-generation"}
        self.assertEqual(self.preflight()["decision"], "canary_required")

    def test_additive_database_changes_pass_on_a_new_batch(self) -> None:
        before = self.preflight()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("ALTER TABLE threads ADD COLUMN future_feature TEXT; CREATE TABLE future_table(id TEXT);")
        after = self.preflight()
        self.assertEqual(after["decision"], "canary_required")
        self.assertNotEqual(before["condition_key"], after["condition_key"])

    def test_missing_required_column_stops_with_exact_reason(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("ALTER TABLE threads RENAME COLUMN archived TO changed_archived")
        report = self.preflight()
        self.assertFalse(report["allow_expensive_inventory"])
        self.assertIn("required cleanup columns missing from threads: archived", report["reasons"])

    def test_failed_migration_still_stops(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE _sqlx_migrations SET success=0")
            connection.commit()
        self.assertIn("failed migration rows are present", self.preflight()["reasons"])

    def test_restart_changes_condition_key_even_for_same_binary(self) -> None:
        before = self.preflight()
        self.process["process_id"] += 1
        self.assertNotEqual(before["condition_key"], self.preflight()["condition_key"])

    def test_discovery_accepts_old_new_and_future_paths_by_signed_parent(self) -> None:
        for path in (
            "C:/Program Files/WindowsApps/OpenAI.Codex_123/app/resources/codex.exe",
            "C:/Users/user/AppData/Local/OpenAI/Codex/bin/new-hash/codex.exe",
            "E:/Applications/next-layout/codex.exe",
        ):
            with self.subTest(path=path), mock.patch.object(compat, "run_powershell_json", return_value=[dict(self.process, executable_path=path)]), mock.patch.object(compat, "authenticode_evidence", return_value=SIGNATURE):
                self.assertEqual(compat.discover_desktop_app_server()["executable_path"], path)

    def attest(self, probe=None, processes=None) -> dict:
        with (
            mock.patch.object(compat, "discover_desktop_app_server", side_effect=processes or [self.process, self.process]),
            mock.patch.object(compat, "authenticode_evidence", return_value=SIGNATURE),
            mock.patch.object(compat, "read_codex_version", return_value=self.runtime["cli"]),
            mock.patch.object(compat, "inspect_protocol_capabilities", side_effect=probe or (lambda _: self.runtime["capabilities"])),
        ):
            return compat.attest_desktop_runtime(self.home)

    def test_current_backend_does_not_need_a_plugin_mirror(self) -> None:
        self.assertFalse((self.home / "plugins").exists())
        report = self.attest()
        self.assertTrue(report["ok"], report["reason"])
        self.assertEqual(report["executable"]["path"], str(self.executable))

    def test_runtime_file_change_during_probe_stops(self) -> None:
        def change(_executable: Path) -> dict:
            self.executable.write_bytes(b"replacement binary")
            return self.runtime["capabilities"]
        report = self.attest(probe=change)
        self.assertFalse(report["ok"])
        self.assertIn("changed during attestation", report["reason"])

    def test_process_change_during_probe_stops(self) -> None:
        report = self.attest(processes=[self.process, dict(self.process, process_id=1235)])
        self.assertFalse(report["ok"])

    def test_missing_capability_stops_before_any_delete(self) -> None:
        report = self.attest(probe=lambda _: {"ok": False, "problems": ["thread/delete missing"]})
        self.assertFalse(report["ok"])
        self.assertIn("thread/delete missing", report["reason"])

    def test_protocol_probe_uses_isolated_configuration_and_no_rpc(self) -> None:
        def generate(command: list[str], **kwargs: object) -> mock.Mock:
            self.assertEqual(command[1:4], ["app-server", "generate-json-schema", "--experimental"])
            self.assertNotEqual(kwargs["env"]["CODEX_HOME"], str(self.home))
            output = Path(command[-1])
            output.mkdir()
            (output / "ClientRequest.json").write_text(json.dumps(request_schema()), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(compat.subprocess, "run", side_effect=generate):
            self.assertTrue(compat.inspect_protocol_capabilities(self.executable)["ok"])


class ProtocolShapeTests(unittest.TestCase):
    def test_local_refs_and_single_allof_keep_the_same_capability(self) -> None:
        schema = request_schema()
        schema["oneOf"][1]["properties"]["params"] = {"allOf": [{"$ref": "#/definitions/Delete"}]}
        self.assertEqual(compat.protocol_shape_problems(schema), [])
        schema["oneOf"][1]["properties"]["params"]["required"] = ["newRequiredField"]
        self.assertIn("thread/delete", compat.protocol_shape_problems(schema)[0])

    def test_current_and_additive_api_shapes_pass(self) -> None:
        schema = request_schema()
        self.assertEqual(compat.protocol_shape_problems(schema), [])
        schema["definitions"]["Delete"]["properties"]["optionalFutureField"] = {"type": "string"}
        schema["oneOf"].append({"type": "object", "properties": {"method": {"const": "future/api"}}})
        self.assertEqual(compat.protocol_shape_problems(schema), [])

    def test_new_required_parameter_is_a_real_breaking_change(self) -> None:
        schema = request_schema()
        schema["definitions"]["Delete"]["required"].append("newRequiredField")
        self.assertEqual(len(compat.protocol_shape_problems(schema)), 1)

    def test_renamed_or_removed_delete_method_stops(self) -> None:
        schema = request_schema()
        schema["oneOf"][1]["properties"]["method"]["const"] = "thread/erase"
        self.assertIn("thread/delete", compat.protocol_shape_problems(schema)[0])

    def test_changed_thread_id_type_stops(self) -> None:
        schema = request_schema()
        schema["definitions"]["Delete"]["properties"]["threadId"] = {"type": "integer"}
        self.assertIn("thread/delete", compat.protocol_shape_problems(schema)[0])

    def test_undeclared_thread_id_is_not_proved_by_additional_properties(self) -> None:
        schema = request_schema()
        schema["definitions"]["Delete"] = {"type": "object", "additionalProperties": True}
        self.assertIn("thread/delete", compat.protocol_shape_problems(schema)[0])


if __name__ == "__main__":
    unittest.main()
