from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from robot_trials.api import JsonApplication
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TrialService(self.connection)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
        ):
            self.service.create_user(user_id, user_id, role)
        protocol = json.loads((ROOT / "fixtures" / "demo_protocol.json").read_text(encoding="utf-8"))
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
        self.service.publish_protocol("stat", protocol)
        self.service.create_batch("operator", "batch-a", "demo-delivery-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def _import(self, rows: list[dict[str, object]], key: str = "k1"):
        body = json.dumps({"observations": rows}).encode("utf-8")
        return self.app.handle(
            "POST",
            "/batches/batch-a/observations",
            headers={"X-Actor-Id": "operator", "Idempotency-Key": key},
            body=body,
        )

    def test_invalid_observation_points_at_specific_field(self) -> None:
        rows = [dict(item) for item in self.rows]
        rows[4] = dict(rows[4])
        rows[4]["metrics"] = dict(rows[4]["metrics"])
        rows[4]["metrics"]["interventions"] = -1
        response = self._import(rows)
        self.assertEqual(response.status, 422)
        error = response.body["error"]
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(
            error["details"], {"index": 4, "field": "observation.metrics.interventions"}
        )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM observations").fetchone()[0], 0)

    def test_unparseable_timestamp_is_rejected_over_http(self) -> None:
        rows = [dict(item) for item in self.rows]
        rows[0] = dict(rows[0])
        rows[0]["observed_at"] = "现场第三班"
        response = self._import(rows)
        self.assertEqual(response.status, 422)
        self.assertEqual(
            response.body["error"]["details"],
            {"index": 0, "field": "observation.observed_at"},
        )

    def test_legal_import_and_replay(self) -> None:
        first = self._import(self.rows)
        self.assertEqual(first.status, 200)
        second = self._import(self.rows)
        self.assertEqual(second.status, 200)
        self.assertEqual(second.body, first.body)
        stored = self.connection.execute("SELECT observed_at FROM observations LIMIT 1").fetchone()[0]
        self.assertEqual(stored, "2026-09-21T01:00:00Z")


if __name__ == "__main__":
    unittest.main()
