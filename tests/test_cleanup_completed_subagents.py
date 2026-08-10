from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_completed_subagents.py"
SPEC = importlib.util.spec_from_file_location("cleanup_completed_subagents", SCRIPT)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class FakeServer:
    def __init__(self, _executable: Path, codex_home: Path, _timeout: float) -> None:
        self.codex_home = codex_home
        self.responses: dict[str, dict] = {}
        self.mutations: dict[str, str] = {}
        self.calls: list[str] = []

    def rpc(self, _request_id: int, _method: str, params: dict) -> dict:
        root_id = str(params["threadId"])
        self.calls.append(root_id)
        mutation = self.mutations.get(root_id)
        rollout = self.codex_home / "sessions" / f"{root_id}.jsonl"
        if mutation == "delete":
            with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
                connection.execute("DELETE FROM thread_spawn_edges WHERE child_thread_id=?", (root_id,))
                connection.execute("DELETE FROM threads WHERE id=?", (root_id,))
                connection.commit()
            rollout.unlink()
        elif mutation == "file_only":
            rollout.unlink()
        return self.responses.get(root_id, {"id": 1, "result": {}})

    def close(self) -> None:
        return


class CleanupCompletedSubagentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "CodexHome"
        (self.codex_home / "sessions").mkdir(parents=True)
        (self.codex_home / "thread-writer-locks").mkdir()
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    updated_at_ms INTEGER,
                    archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                """
            )
        self.results_path = self.root / "results.json"
        self.plan_path = self.root / "plan.json"
        self.executable = self.root / "codex.exe"
        self.executable.write_bytes(b"test")
        self.protected: set[str] = set()
        self.fingerprint = {
            "condition_key": "condition",
            "protected_ids_sha256": cleanup.canonical_hash([]),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_root(self, root_id: str, *, status: str = "completed") -> dict:
        rollout = self.codex_home / "sessions" / f"{root_id}.jsonl"
        rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
        metadata = rollout.stat()
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "INSERT INTO threads(id, rollout_path, updated_at, updated_at_ms, archived) "
                "VALUES(?,?,?,?,0)",
                (root_id, str(rollout), 10, 10000),
            )
            connection.commit()
        return {
            "root_id": root_id,
            "thread_ids": [root_id],
            "thread_count": 1,
            "size_bytes": metadata.st_size,
            "files": [
                {
                    "thread_id": root_id,
                    "path": str(rollout.resolve()),
                    "size_bytes": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                }
            ],
            "rows": [
                {
                    "thread_id": root_id,
                    "updated_at": 10,
                    "updated_at_ms": 10000,
                    "archived": 0,
                    "edge_status": "",
                }
            ],
        }

    def run_plan(self, roots: list[dict], server: FakeServer) -> dict:
        plan = {"roots": roots, "initial_skips": []}
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with mock.patch.object(
            cleanup, "check_light_fingerprint", return_value={"condition_key": "condition"}
        ):
            return cleanup.execute_roots(
                plan,
                self.plan_path,
                self.results_path,
                self.codex_home,
                self.protected,
                self.fingerprint,
                self.executable,
                1.0,
                server_factory=lambda *_args: server,
            )

    def test_exact_active_writer_skips_root_and_continues(self) -> None:
        first = self.add_root("00000000-0000-0000-0000-000000000001")
        second = self.add_root("00000000-0000-0000-0000-000000000002")
        server = FakeServer(self.executable, self.codex_home, 1.0)
        server.responses[first["root_id"]] = {
            "id": 100,
            "error": {"code": -32600, "message": "thread already has an active writer"},
        }
        server.mutations[second["root_id"]] = "delete"

        results = self.run_plan([first, second], server)

        self.assertEqual(server.calls, [first["root_id"], second["root_id"]])
        self.assertEqual([item["outcome"] for item in results["entries"]], ["skipped", "deleted"])
        self.assertEqual(results["entries"][0]["reason"], "active_writer")
        self.assertTrue(results["entries"][1]["canary"])

    def test_existing_writer_lock_skips_without_rpc_and_continues(self) -> None:
        first = self.add_root("00000000-0000-0000-0000-000000000003")
        second = self.add_root("00000000-0000-0000-0000-000000000004")
        (self.codex_home / "thread-writer-locks" / f"{first['root_id']}.lock").write_text("busy")
        server = FakeServer(self.executable, self.codex_home, 1.0)
        server.mutations[second["root_id"]] = "delete"

        results = self.run_plan([first, second], server)

        self.assertEqual(server.calls, [second["root_id"]])
        self.assertEqual(results["entries"][0]["reason"], "writer_lock")
        self.assertEqual(results["entries"][1]["outcome"], "deleted")

    def test_mtime_change_skips_root_and_continues(self) -> None:
        first = self.add_root("00000000-0000-0000-0000-000000000005")
        second = self.add_root("00000000-0000-0000-0000-000000000006")
        changed = Path(first["files"][0]["path"])
        old = changed.stat().st_mtime_ns
        os.utime(changed, ns=(old + 1_000_000_000, old + 1_000_000_000))
        server = FakeServer(self.executable, self.codex_home, 1.0)
        server.mutations[second["root_id"]] = "delete"

        results = self.run_plan([first, second], server)

        self.assertEqual(server.calls, [second["root_id"]])
        self.assertEqual(results["entries"][0]["reason"], "rollout_mtime_changed")
        self.assertEqual(results["entries"][1]["outcome"], "deleted")

    def test_unknown_rpc_error_stops_batch(self) -> None:
        first = self.add_root("00000000-0000-0000-0000-000000000007")
        second = self.add_root("00000000-0000-0000-0000-000000000008")
        server = FakeServer(self.executable, self.codex_home, 1.0)
        server.responses[first["root_id"]] = {
            "id": 100,
            "error": {"code": -32000, "message": "unexpected"},
        }

        with self.assertRaises(cleanup.BatchStop) as raised:
            self.run_plan([first, second], server)

        self.assertEqual(raised.exception.reason, "unknown_rpc_error")
        self.assertEqual(server.calls, [first["root_id"]])

    def test_partial_deletion_stops_batch(self) -> None:
        first = self.add_root("00000000-0000-0000-0000-000000000009")
        second = self.add_root("00000000-0000-0000-0000-000000000010")
        server = FakeServer(self.executable, self.codex_home, 1.0)
        server.mutations[first["root_id"]] = "file_only"

        with self.assertRaises(cleanup.PartialDeletion):
            self.run_plan([first, second], server)

        self.assertEqual(server.calls, [first["root_id"]])

    def test_incomplete_global_status_source_stops_before_selection(self) -> None:
        evidence = self.root / "status.json"
        evidence.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "captured_at": cleanup.now_iso(),
                    "global_complete": False,
                    "unavailable_sources": ["host"],
                    "threads": {},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(cleanup.BatchStop) as raised:
            cleanup.validate_status_evidence(evidence, 900)

        self.assertEqual(raised.exception.reason, "global_status_source_failure")

    def test_resume_accepts_only_monotonic_protection_growth(self) -> None:
        root = self.add_root("00000000-0000-0000-0000-000000000011")
        (self.codex_home / "thread-writer-locks" / f"{root['root_id']}.lock").write_text("busy")
        server = FakeServer(self.executable, self.codex_home, 1.0)
        self.run_plan([root], server)
        plan = {"roots": [root], "initial_skips": [], "protected_ids": []}
        protected = {"00000000-0000-0000-0000-000000000099"}
        fingerprint = {
            "condition_key": "condition",
            "protected_ids_sha256": cleanup.canonical_hash(sorted(protected)),
        }

        results = cleanup.execute_roots(
            plan,
            self.plan_path,
            self.results_path,
            self.codex_home,
            protected,
            fingerprint,
            self.executable,
            1.0,
            server_factory=lambda *_args: server,
        )

        self.assertEqual(set(results["protected_ids"]), protected)


if __name__ == "__main__":
    unittest.main()
