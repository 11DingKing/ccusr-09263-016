"""HTTP 接口边界：路由、幂等头、错误映射与端到端流程。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service_09252_008.interfaces.http_api import create_server
from tests.helpers import FlakyNotificationPort, apply_payload, make_services, seed_catalog


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog, bookings, clock, store = make_services()
        cls.ids = seed_catalog(catalog)
        cls.server = create_server("127.0.0.1", 0, catalog, bookings)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_booking_flow_over_http_with_idempotency_header(self) -> None:
        apply_body = {
            "institution": "港城理工学院",
            "package_id": self.ids["package_id"],
            "mentor_id": self.ids["mentor_id"],
            "resource_id": self.ids["resource_id"],
            "window_id": self.ids["window_id"],
            "seats": 6,
            "slot_start": "2026-10-01T02:00:00+00:00",
            "slot_end": "2026-10-01T04:00:00+00:00",
        }
        status, applied = self._request(
            "POST", "/bookings", apply_body, headers={"Idempotency-Key": "http-apply-1"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(applied["status"], "REQUESTED")
        booking_id = applied["booking_id"]

        # 相同幂等键重放：不产生新预约
        status, replay = self._request(
            "POST", "/bookings", apply_body, headers={"Idempotency-Key": "http-apply-1"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(replay["booking_id"], booking_id)
        self.assertTrue(replay["idempotent_replay"])

        # 同一键不同载荷 -> 409
        status, conflict = self._request(
            "POST", "/bookings", {**apply_body, "seats": 8}, headers={"Idempotency-Key": "http-apply-1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "idempotency_conflict")

        status, quoted = self._request("POST", f"/bookings/{booking_id}/quote", {})
        self.assertEqual(status, 200)
        self.assertEqual(quoted["status"], "QUOTED")

        status, locked = self._request(
            "POST", f"/bookings/{booking_id}/lock", {}, headers={"Idempotency-Key": "http-lock-1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(locked["status"], "LOCKED")

        status, fetched = self._request("GET", f"/bookings/{booking_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["booking_id"], booking_id)
        self.assertEqual(len(fetched["reservations"]), 2)

    def test_error_mapping(self) -> None:
        status, body = self._request("GET", "/bookings/bkg_missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

        status, body = self._request("POST", "/bookings", {"institution": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")

        status, body = self._request("POST", "/no-such-route", {})
        self.assertEqual(status, 404)

    def test_admin_recover_endpoint(self) -> None:
        status, body = self._request("POST", "/admin/recover", {})
        self.assertEqual(status, 200)
        self.assertIn("expired_locks", body)
        self.assertIn("expired_quotes", body)


class CancellationCompensationHttpTests(unittest.TestCase):
    """取消补偿编排的 HTTP 边界：部分失败、重复取消、失败重试。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.notifications = FlakyNotificationPort(fail_mentor_times=1)
        catalog, bookings, clock, store = make_services(notification_port=cls.notifications)
        cls.ids = seed_catalog(catalog)
        cls.applied = bookings.apply(apply_payload(cls.ids, "http-cancel-1"))
        cls.server = create_server("127.0.0.1", 0, catalog, bookings)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_cancel_partial_failure_then_retry_and_duplicate_rejected(self) -> None:
        booking_id = self.applied["booking_id"]

        status, cancelled = self._request(
            "POST", f"/bookings/{booking_id}/cancel", {"reason": "plan_changed"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["compensation_summary"]["status"], "partial")
        self.assertEqual(cancelled["compensation_summary"]["failed"], ["notify_mentor"])
        self.assertEqual(self.notifications.delivered_mentor, 0)

        # GET 可查每个动作结果
        status, fetched = self._request("GET", f"/bookings/{booking_id}")
        self.assertEqual(status, 200)
        by_action = {r["action"]: r for r in fetched["compensation"]}
        self.assertEqual(by_action["notify_mentor"]["status"], "FAILED")
        self.assertEqual(by_action["promote_waitlist"]["status"], "SUCCEEDED")

        # 重复取消被状态机拒绝，不触发任何补偿
        status, error = self._request(
            "POST", f"/bookings/{booking_id}/cancel", {"reason": "plan_changed"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "invalid_state")
        self.assertEqual(len(self.notifications.mentor_calls), 1)

        # 重试失败动作
        status, retried = self._request("POST", f"/bookings/{booking_id}/cancel/retry", {})
        self.assertEqual(status, 200)
        self.assertEqual(retried["compensation_summary"]["status"], "completed")
        self.assertEqual(self.notifications.delivered_mentor, 1)

        # 无失败动作时再重试 -> 409，且不重复外发
        status, error = self._request("POST", f"/bookings/{booking_id}/cancel/retry", {})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "invalid_state")
        self.assertEqual(len(self.notifications.mentor_calls), 2)


if __name__ == "__main__":
    unittest.main()
