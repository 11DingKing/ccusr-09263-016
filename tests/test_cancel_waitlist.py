"""取消规则：释放候补、回补库存、发运后记损耗；已发运预约不可改期。"""
from __future__ import annotations

import unittest

from service_09252_008.domain.errors import BookingImmutableError, StateError
from tests.helpers import apply_payload, batch_available, event_types, make_services, seed_catalog


class CancelAndWaitlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()

    def test_cancel_requested_promotes_waitlist_fifo(self) -> None:
        ids = seed_catalog(self.catalog, window_capacity=1)
        first = self.bookings.apply(apply_payload(ids, "k-cw-1"))
        second = self.bookings.apply(apply_payload(ids, "k-cw-2"))
        third = self.bookings.apply(apply_payload(ids, "k-cw-3"))
        self.assertEqual(second["status"], "WAITLISTED")
        self.assertEqual(third["status"], "WAITLISTED")
        self.assertEqual(third["waitlist_position"], 2)

        self.bookings.cancel(first["booking_id"], {"reason": "计划变更"})
        promoted = self.bookings.get_booking(second["booking_id"])
        self.assertEqual(promoted["status"], "REQUESTED")
        self.assertIn("waitlist_promoted", event_types(promoted))
        still_waiting = self.bookings.get_booking(third["booking_id"])
        self.assertEqual(still_waiting["status"], "WAITLISTED")
        self.assertEqual(still_waiting["waitlist_position"], 1)

    def test_cancel_locked_releases_stock_and_promotes(self) -> None:
        ids = seed_catalog(self.catalog, window_capacity=1)
        first = self.bookings.apply(apply_payload(ids, "k-cl-1"))
        self.bookings.quote(first["booking_id"])
        self.bookings.lock(first["booking_id"], {"idempotency_key": "k-cl-lock"})
        self.assertEqual(batch_available(self.store, ids["dye_batch_id"]), 95.0)
        second = self.bookings.apply(apply_payload(ids, "k-cl-2"))
        self.assertEqual(second["status"], "WAITLISTED")

        cancelled = self.bookings.cancel(first["booking_id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        # 未发运，库存全额回补且无损耗
        self.assertEqual(batch_available(self.store, ids["dye_batch_id"]), 100.0)
        self.assertEqual(batch_available(self.store, ids["cloth_batch_id"]), 100.0)
        self.assertEqual(cancelled["losses"], [])
        self.assertEqual(self.bookings.get_booking(second["booking_id"])["status"], "REQUESTED")

    def test_cancel_after_shipment_records_loss(self) -> None:
        ids = seed_catalog(self.catalog)
        first = self.bookings.apply(apply_payload(ids, "k-cs-1"))
        self.bookings.quote(first["booking_id"])
        self.bookings.lock(first["booking_id"], {"idempotency_key": "k-cs-lock"})
        self.bookings.ship(first["booking_id"], {"idempotency_key": "k-cs-ship"})
        cancelled = self.bookings.cancel(first["booking_id"], {"reason": "院校临时停课"})
        self.assertEqual(cancelled["status"], "CANCELLED")
        by_material = {}
        for loss in cancelled["losses"]:
            self.assertEqual(loss["reason"], "cancel_after_shipment")
            by_material[loss["material_id"]] = loss["quantity"]
        self.assertEqual(by_material, {"dye": 5.0, "cloth": 10.0})
        # 已发运材料不回补库存
        self.assertEqual(batch_available(self.store, ids["dye_batch_id"]), 95.0)
        self.assertEqual(batch_available(self.store, ids["cloth_batch_id"]), 90.0)
        # 发运单被关闭
        self.assertTrue(all(s["status"] == "CLOSED_WITH_LOSS" for s in cancelled["shipments"]))

    def test_shipped_booking_cannot_be_rescheduled(self) -> None:
        ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(ids, "k-rs-1"))
        booking_id = applied["booking_id"]
        # 申请态可改期
        moved = self.bookings.reschedule(
            booking_id,
            {"slot_start": "2026-10-01T05:00:00+00:00", "slot_end": "2026-10-01T07:00:00+00:00"},
        )
        self.assertEqual(moved["slot_start"], "2026-10-01T05:00:00+00:00")
        self.assertEqual(moved["status"], "REQUESTED")
        # 发运后不可移动
        self.bookings.quote(booking_id)
        self.bookings.lock(booking_id, {"idempotency_key": "k-rs-lock"})
        self.bookings.ship(booking_id, {"idempotency_key": "k-rs-ship"})
        with self.assertRaises(BookingImmutableError):
            self.bookings.reschedule(
                booking_id,
                {"slot_start": "2026-10-01T06:00:00+00:00", "slot_end": "2026-10-01T08:00:00+00:00"},
            )

    def test_locked_reschedule_releases_and_replans(self) -> None:
        ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(ids, "k-rl-1"))
        booking_id = applied["booking_id"]
        self.bookings.quote(booking_id)
        self.bookings.lock(booking_id, {"idempotency_key": "k-rl-lock"})
        self.assertEqual(batch_available(self.store, ids["dye_batch_id"]), 95.0)
        moved = self.bookings.reschedule(
            booking_id,
            {"slot_start": "2026-10-01T05:00:00+00:00", "slot_end": "2026-10-01T07:00:00+00:00"},
        )
        self.assertEqual(moved["status"], "REQUESTED")  # 回到待报价
        self.assertIsNone(moved["quote"])
        self.assertEqual(batch_available(self.store, ids["dye_batch_id"]), 100.0)  # 预占已释放

    def test_terminal_states_reject_cancel(self) -> None:
        ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(ids, "k-tc-1"))
        self.bookings.cancel(applied["booking_id"])
        with self.assertRaises(StateError):
            self.bookings.cancel(applied["booking_id"])


if __name__ == "__main__":
    unittest.main()
