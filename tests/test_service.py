from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robot_trials.clock import FrozenClock
from robot_trials.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
        self.service.publish_protocol("stat", self.protocol)
        self.service.create_batch("operator", "batch-a", "demo-delivery-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def test_complete_workflow(self) -> None:
        imported = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker", 30)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["batch"]["state"], "decided")
        self.assertEqual(report["analysis"]["result"]["conclusion"], "pass")

    def test_idempotent_replay_and_conflict(self) -> None:
        first = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        second = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(first, second)
        changed = [dict(item) for item in self.rows]
        changed[0] = dict(changed[0])
        changed[0]["metrics"] = dict(changed[0]["metrics"])
        changed[0]["metrics"]["completion_seconds"] = "99"
        with self.assertRaises(Conflict):
            self.service.import_observations("operator", "batch-a", "key-1", changed)
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 6)

    def test_import_rolls_back_when_one_source_row_duplicates(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows[:1])
        with self.assertRaises(Conflict):
            self.service.import_observations("operator", "batch-a", "key-2", self.rows[:2])
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_row_leaves_no_residue(self) -> None:
        def residue() -> tuple[int, int, int]:
            return (
                self.connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                self.connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0],
                self.connection.execute(
                    "SELECT count(*) FROM audit_events WHERE event_type='observations.imported'"
                ).fetchone()[0],
            )

        cases = (
            ("bad-time", {"observed_at": "无法解析的时间"}),
            ("naive-time", {"observed_at": "2026-09-21T09:00:00"}),
            (
                "negative-count",
                {"metrics": {"completed": 1, "completion_seconds": "42.8", "interventions": -1}},
            ),
        )
        for index, (key, overrides) in enumerate(cases):
            bad = [dict(self.rows[0]) | {"source_row": f"bad-{key}"}]
            bad[0].update(overrides)
            with self.subTest(key=key):
                with self.assertRaises(ValidationFailed) as caught:
                    self.service.import_observations("operator", "batch-a", key, bad)
                self.assertIsNotNone(caught.exception.field)
            self.assertEqual(residue(), (0, 0, 0))

        # 第二行非法时，第一行合法数据也不得残留。
        mixed = [
            dict(self.rows[0]) | {"source_row": "mix-1"},
            dict(self.rows[1]) | {"source_row": "mix-2", "observed_at": "not-a-time"},
        ]
        with self.assertRaises(ValidationFailed) as caught:
            self.service.import_observations("operator", "batch-a", "mixed-key", mixed)
        self.assertEqual(caught.exception.field, "observations[1].observed_at")
        self.assertEqual(residue(), (0, 0, 0))

        # 失败的幂等键可在数据修正后用于合法导入。
        fixed = [dict(mixed[1]) | {"observed_at": "2026-09-21T09:10:00+08:00"}]
        result = self.service.import_observations("operator", "batch-a", "mixed-key", fixed)
        self.assertEqual(result["inserted"], 1)

    def test_invalid_row_field_points_to_specific_row_and_field(self) -> None:
        bad = [dict(self.rows[0]) | {
            "source_row": "neg",
            "metrics": {"completed": 1, "completion_seconds": "42.8", "interventions": -2},
        }]
        with self.assertRaises(ValidationFailed) as caught:
            self.service.import_observations("operator", "batch-a", "neg-key", bad)
        self.assertEqual(caught.exception.field, "observations[0].metrics.interventions")

    def test_valid_import_normalizes_observed_at_to_utc(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows[:1])
        stored = self.connection.execute(
            "SELECT observed_at FROM observations WHERE source_row='001'"
        ).fetchone()[0]
        self.assertEqual(stored, "2026-09-21T01:00:00Z")

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.seal_batch("operator", "batch-a", 2)
        with self.assertRaises(Forbidden):
            self.service.report("operator", "batch-a")

    def test_exclusion_review_and_revoke_leave_history(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations ORDER BY observation_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", observation_id, "现场记录失效")
        reviewed = self.service.review_exclusion("stat", requested["exclusion_id"], True, "证据充分")
        self.assertEqual(reviewed["status"], "approved")
        revoked = self.service.revoke_exclusion("operator", requested["exclusion_id"], "已找回原始记录")
        self.assertEqual(revoked["status"], "revoked")
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='observation' AND entity_id=? ORDER BY event_id",
            (str(observation_id),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["exclusion.requested", "exclusion.revoked"])

    def test_failed_job_returns_to_queue_after_delay(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker-a", 10)
        failed = self.service.fail_job("worker-a", job["job_id"], "临时计算失败", retry_seconds=5)
        self.assertEqual(failed["state"], "queued")
        self.assertIsNone(self.service.claim_job("worker-b", 10))
        self.clock.advance(seconds=5)
        retried = self.service.claim_job("worker-b", 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_lease_can_be_reclaimed_after_expiry(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        first = self.service.claim_job("worker-a", 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("worker-b", 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["lease_owner"], "worker-b")
        with self.assertRaises(InvalidState):
            self.service.complete_job("worker-a", first["job_id"], "stat")


if __name__ == "__main__":
    unittest.main()
