"""取消补偿编排：按原因选择动作、逐动作留痕、失败可重试、补偿不重复发放。"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.cancellation import (
    ACTION_CLOSE_SHIPMENTS,
    ACTION_ESCALATE_SAFETY,
    ACTION_NOTIFY_MENTOR,
    ACTION_PROMOTE_WAITLIST,
    ACTION_RELEASE_RESERVATIONS,
    ACTION_WRITE_OFF_SHIPPED,
    REASON_PLAN_CHANGED,
    REASON_SAFETY_INCIDENT,
    REASON_WAITLIST_ABANDONED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    list_notifications,
)
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.errors import StateError
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import (
    NOW,
    FlakyNotificationPort,
    apply_payload,
    batch_available,
    make_services,
    seed_catalog,
)


def compensation_by_action(view: dict) -> dict[str, dict]:
    return {record["action"]: record for record in view["compensation"]}


class CancellationReasonSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notifications = FlakyNotificationPort()
        self.catalog, self.bookings, self.clock, self.store = make_services(
            notification_port=self.notifications
        )

    def test_plan_changed_notifies_mentor_and_promotes(self) -> None:
        ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(ids, "k-rc-1"))
        view = self.bookings.cancel(applied["booking_id"], {"reason": REASON_PLAN_CHANGED})
        by_action = compensation_by_action(view)
        self.assertEqual(view["compensation_summary"]["status"], "completed")
        self.assertEqual(list(by_action), [ACTION_PROMOTE_WAITLIST, ACTION_NOTIFY_MENTOR])
        self.assertTrue(all(r["status"] == STATUS_SUCCEEDED for r in view["compensation"]))
        self.assertEqual(self.notifications.delivered_mentor, 1)

    def test_safety_incident_adds_escalation(self) -> None:
        ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(ids, "k-rc-2"))
        view = self.bookings.cancel(applied["booking_id"], {"reason": REASON_SAFETY_INCIDENT})
        actions = [r["action"] for r in view["compensation"]]
        self.assertEqual(
            actions,
            [ACTION_PROMOTE_WAITLIST, ACTION_NOTIFY_MENTOR, ACTION_ESCALATE_SAFETY],
        )
        self.assertEqual(self.notifications.delivered_mentor, 1)
        self.assertEqual(self.notifications.delivered_escalation, 1)

    def test_waitlist_abandoned_runs_no_notification(self) -> None:
        ids = seed_catalog(self.catalog, window_capacity=1)
        first = self.bookings.apply(apply_payload(ids, "k-rc-w1"))
        second = self.bookings.apply(apply_payload(ids, "k-rc-w2"))
        self.assertEqual(second["status"], "WAITLISTED")
        view = self.bookings.cancel(second["booking_id"], {"reason": REASON_WAITLIST_ABANDONED})
        self.assertEqual([r["action"] for r in view["compensation"]], [ACTION_PROMOTE_WAITLIST])
        self.assertEqual(self.notifications.mentor_calls, [])
        self.assertEqual(self.notifications.escalation_calls, [])
        # 占用窗口的第一笔预约不受影响
        self.assertEqual(self.bookings.get_booking(first["booking_id"])["status"], "REQUESTED")

    def test_unknown_free_text_reason_falls_back_to_default(self) -> None:
        ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(ids, "k-rc-3"))
        view = self.bookings.cancel(applied["booking_id"], {"reason": "院校临时停课"})
        actions = [r["action"] for r in view["compensation"]]
        self.assertEqual(actions, [ACTION_PROMOTE_WAITLIST, ACTION_NOTIFY_MENTOR])
        cancelled_event = next(e for e in view["events"] if e["type"] == "booking_cancelled")
        self.assertEqual(cancelled_event["payload"]["reason"], REASON_PLAN_CHANGED)
        self.assertEqual(cancelled_event["payload"]["raw_reason"], "院校临时停课")


class PartialFailureAndRetryTests(unittest.TestCase):
    def _shipped_booking_with_waitlist(self, notifications: FlakyNotificationPort) -> tuple[dict, dict, str]:
        catalog, bookings, clock, store = make_services(notification_port=notifications)
        ids = seed_catalog(catalog, window_capacity=1)
        first = bookings.apply(apply_payload(ids, "k-pf-1"))
        bookings.quote(first["booking_id"])
        bookings.lock(first["booking_id"], {"idempotency_key": "k-pf-lock"})
        bookings.ship(first["booking_id"], {"idempotency_key": "k-pf-ship"})
        second = bookings.apply(apply_payload(ids, "k-pf-2"))
        self.assertEqual(second["status"], "WAITLISTED")
        return ids, {"catalog": catalog, "bookings": bookings, "store": store}, first["booking_id"]

    def test_partial_action_failure_is_recorded_and_other_actions_still_apply(self) -> None:
        notifications = FlakyNotificationPort(fail_mentor_times=1)
        ids, services, booking_id = self._shipped_booking_with_waitlist(notifications)
        bookings = services["bookings"]
        store = services["store"]

        view = bookings.cancel(booking_id, {"reason": REASON_PLAN_CHANGED})
        self.assertEqual(view["compensation_summary"]["status"], "partial")
        self.assertEqual(view["compensation_summary"]["failed"], [ACTION_NOTIFY_MENTOR])

        by_action = compensation_by_action(view)
        self.assertEqual(
            list(by_action),
            [
                ACTION_RELEASE_RESERVATIONS,
                ACTION_WRITE_OFF_SHIPPED,
                ACTION_CLOSE_SHIPMENTS,
                ACTION_PROMOTE_WAITLIST,
                ACTION_NOTIFY_MENTOR,
            ],
        )
        self.assertEqual(by_action[ACTION_NOTIFY_MENTOR]["status"], STATUS_FAILED)
        self.assertEqual(by_action[ACTION_NOTIFY_MENTOR]["attempts"], 1)
        self.assertIn("mentor gateway unavailable", by_action[ACTION_NOTIFY_MENTOR]["error"])
        for action in (
            ACTION_RELEASE_RESERVATIONS,
            ACTION_WRITE_OFF_SHIPPED,
            ACTION_CLOSE_SHIPMENTS,
            ACTION_PROMOTE_WAITLIST,
        ):
            self.assertEqual(by_action[action]["status"], STATUS_SUCCEEDED)

        # 成功的动作确实生效：损耗、关单、候补晋级
        losses = {(l["material_id"], l["reason"]): l["quantity"] for l in view["losses"]}
        self.assertEqual(
            losses,
            {("dye", "cancel_after_shipment"): 5.0, ("cloth", "cancel_after_shipment"): 10.0},
        )
        self.assertTrue(all(s["status"] == "CLOSED_WITH_LOSS" for s in view["shipments"]))
        self.assertEqual(view["status"], "CANCELLED")
        # 通知只尝试过一次且失败，无任何成功外发
        self.assertEqual(len(notifications.mentor_calls), 1)
        self.assertEqual(notifications.delivered_mentor, 0)

    def test_second_cancel_and_retry_do_not_reissue_completed_actions(self) -> None:
        notifications = FlakyNotificationPort(fail_mentor_times=1)
        ids, services, booking_id = self._shipped_booking_with_waitlist(notifications)
        bookings = services["bookings"]
        store = services["store"]

        first_view = bookings.cancel(booking_id, {"reason": REASON_PLAN_CHANGED})
        self.assertEqual(first_view["compensation_summary"]["status"], "partial")

        # 重复取消：终态直接拒绝，任何补偿都不再执行
        with self.assertRaises(StateError):
            bookings.cancel(booking_id, {"reason": REASON_PLAN_CHANGED})
        self.assertEqual(len(notifications.mentor_calls), 1)

        # 重试：只重跑失败的通知，库存/损耗/关单/候补动作不得重复执行
        retried = bookings.retry_cancel_compensation(booking_id)
        self.assertEqual(retried["compensation_summary"]["status"], "completed")
        self.assertEqual(
            [r["action"] for r in retried["compensation_summary"]["results"]],
            [ACTION_NOTIFY_MENTOR],
        )
        by_action = compensation_by_action(bookings.get_booking(booking_id))
        self.assertEqual(by_action[ACTION_NOTIFY_MENTOR]["status"], STATUS_SUCCEEDED)
        self.assertEqual(by_action[ACTION_NOTIFY_MENTOR]["attempts"], 2)

        # 通知失败一次、重试成功一次，最终只送达一份
        self.assertEqual(len(notifications.mentor_calls), 2)
        self.assertEqual(notifications.delivered_mentor, 1)

        # 损耗仍是首次取消时的两条，库存/关单未被重试重复改动
        fresh = bookings.get_booking(booking_id)
        self.assertEqual(len(fresh["losses"]), 2)
        self.assertEqual(batch_available(store, ids["dye_batch_id"]), 95.0)
        self.assertEqual(batch_available(store, ids["cloth_batch_id"]), 90.0)
        self.assertEqual(sum(1 for s in fresh["shipments"] if s["status"] == "CLOSED_WITH_LOSS"), 2)

        # 全部成功后再次重试应被拒绝
        with self.assertRaises(StateError):
            bookings.retry_cancel_compensation(booking_id)
        self.assertEqual(len(notifications.mentor_calls), 2)

    def test_successful_cancel_never_reissues_on_duplicate_cancel(self) -> None:
        notifications = FlakyNotificationPort()
        catalog, bookings, clock, store = make_services(notification_port=notifications)
        ids = seed_catalog(catalog)
        applied = bookings.apply(apply_payload(ids, "k-pf-ok"))
        bookings.cancel(applied["booking_id"], {"reason": REASON_PLAN_CHANGED})
        self.assertEqual(notifications.delivered_mentor, 1)
        with self.assertRaises(StateError):
            bookings.cancel(applied["booking_id"], {"reason": REASON_PLAN_CHANGED})
        view = bookings.get_booking(applied["booking_id"])
        self.assertEqual(len(list_notifications(store, applied["booking_id"])), 0)  # 外部端口不留本地通知
        self.assertEqual(notifications.delivered_mentor, 1)
        self.assertTrue(all(r["status"] == STATUS_SUCCEEDED for r in view["compensation"]))


class CompensationSQLitePersistenceTests(unittest.TestCase):
    def test_every_action_result_persisted_and_retry_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)

            # 第一次“进程”：发运后取消，导师通知故障 -> 部分失败
            store = SQLiteStore(db_path)
            catalog = CatalogService(store, clock, UuidIdGenerator())
            flaky = FlakyNotificationPort(fail_mentor_times=1)
            bookings = BookingService(store, clock, UuidIdGenerator(), notification_port=flaky)
            ids = seed_catalog(catalog, window_capacity=1)
            first = bookings.apply(apply_payload(ids, "k-sql-1"))
            bookings.quote(first["booking_id"])
            bookings.lock(first["booking_id"], {"idempotency_key": "k-sql-lock"})
            bookings.ship(first["booking_id"], {"idempotency_key": "k-sql-ship"})
            second = bookings.apply(apply_payload(ids, "k-sql-2"))
            cancelled = bookings.cancel(first["booking_id"], {"reason": REASON_PLAN_CHANGED})
            self.assertEqual(cancelled["compensation_summary"]["status"], "partial")
            persisted = store.query("cancellation_compensations", booking_id=first["booking_id"])
            self.assertEqual(len(persisted), 5)
            self.assertEqual({r["action"] for r in persisted}, {
                ACTION_RELEASE_RESERVATIONS,
                ACTION_WRITE_OFF_SHIPPED,
                ACTION_CLOSE_SHIPMENTS,
                ACTION_PROMOTE_WAITLIST,
                ACTION_NOTIFY_MENTOR,
            })
            store.close()

            # 第二次“进程”：动作结果从 SQLite 读回，失败项可在重启后重试
            store2 = SQLiteStore(db_path)
            catalog2 = CatalogService(store2, clock, UuidIdGenerator())
            healthy = FlakyNotificationPort()
            bookings2 = BookingService(store2, clock, UuidIdGenerator(), notification_port=healthy)

            loaded = bookings2.get_booking(first["booking_id"])
            self.assertEqual(loaded["status"], "CANCELLED")
            by_action = compensation_by_action(loaded)
            self.assertEqual(by_action[ACTION_NOTIFY_MENTOR]["status"], STATUS_FAILED)
            self.assertEqual(by_action[ACTION_WRITE_OFF_SHIPPED]["status"], STATUS_SUCCEEDED)
            self.assertEqual(len(loaded["losses"]), 2)

            retried = bookings2.retry_cancel_compensation(first["booking_id"])
            self.assertEqual(retried["compensation_summary"]["status"], "completed")
            self.assertEqual(healthy.delivered_mentor, 1)

            # 重启后重复取消仍被拒绝，已发补偿不重复发放
            with self.assertRaises(StateError):
                bookings2.cancel(first["booking_id"], {"reason": REASON_PLAN_CHANGED})
            self.assertEqual(len(healthy.mentor_calls), 1)
            # 候补晋级在第一次取消时已落盘
            self.assertEqual(bookings2.get_booking(second["booking_id"])["status"], "REQUESTED")
            store2.close()


if __name__ == "__main__":
    unittest.main()
