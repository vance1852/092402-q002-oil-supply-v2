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
        self.app = JsonApplication(TrialService(self.connection))

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


class ObservationImportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TrialService(self.connection))
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
        ):
            self.app.handle(
                "POST", "/users",
                body=json.dumps({"user_id": user_id, "display_name": user_id, "role": role}).encode(),
            )
        protocol = json.loads((ROOT / "fixtures" / "demo_protocol.json").read_text(encoding="utf-8"))
        self.app.handle("POST", "/protocols", {"X-Actor-Id": "stat"},
                        json.dumps(protocol).encode())
        self.app.handle("POST", "/robots", {"X-Actor-Id": "operator"},
                        json.dumps({"robot_id": "robot-a", "model_name": "A", "vendor": "v"}).encode())
        self.app.handle(
            "POST", "/builds", {"X-Actor-Id": "operator"},
            json.dumps({"build_id": "build-a", "robot_id": "robot-a", "version": "1",
                        "content_sha256": "b" * 64}).encode(),
        )
        self.app.handle(
            "POST", "/batches", {"X-Actor-Id": "operator"},
            json.dumps({"batch_id": "b1", "protocol_id": "demo-delivery-v1",
                        "protocol_version": 1, "build_id": "build-a"}).encode(),
        )
        self.app.handle("POST", "/batches/b1/start", {"X-Actor-Id": "operator"},
                        json.dumps({"expected_revision": 1}).encode())
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, payload: dict, key: str = "k1"):
        return self.app.handle(
            "POST", "/batches/b1/observations",
            {"X-Actor-Id": "operator", "Idempotency-Key": key},
            json.dumps(payload).encode(),
        )

    def test_missing_idempotency_key_names_header(self) -> None:
        response = self.app.handle(
            "POST", "/batches/b1/observations", {"X-Actor-Id": "operator"},
            json.dumps({"observations": self.rows}).encode(),
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["field"], "Idempotency-Key")

    def test_invalid_row_error_is_stable_and_names_field(self) -> None:
        bad = [dict(self.rows[0]) | {
            "source_row": "z1",
            "metrics": {"completed": 1, "completion_seconds": "42.8", "interventions": -5},
        }]
        response = self._post({"observations": bad}, key="bad")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        self.assertEqual(response.body["error"]["field"], "observations[0].metrics.interventions")
        # 整次失败不留残片。
        self.assertEqual(self.connection.execute("SELECT count(*) FROM observations").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0], 0)

    def test_valid_replay_returns_same_result(self) -> None:
        first = self._post({"observations": self.rows})
        second = self._post({"observations": self.rows})
        self.assertEqual(first.status, 200)
        self.assertEqual(second.body, first.body)


if __name__ == "__main__":
    unittest.main()
