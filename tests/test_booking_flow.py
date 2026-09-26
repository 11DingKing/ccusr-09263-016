"""端到端主流程：申请 -> 报价 -> 锁定 -> 发运 -> 到货 -> 签到 -> 结算。"""
from __future__ import annotations

import unittest
from datetime import datetime

from service_09252_008.domain.errors import StateError
from tests.helpers import (
    SLOT_START,
    apply_payload,
    batch_available,
    event_types,
    make_services,
    seed_catalog,
)


class BookingFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_catalog(self.catalog)

    def test_full_lifecycle(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-apply-1"))
        self.assertEqual(applied["status"], "REQUESTED")
        self.assertEqual(applied["institution"], "城北学院")
        self.assertEqual(len(applied["material_plan"]), 2)
        booking_id = applied["booking_id"]

        quoted = self.bookings.quote(booking_id)
        self.assertEqual(quoted["status"], "QUOTED")
        quote = quoted["quote"]
        self.assertEqual(quote["mentor_fee_cents"], 16000)  # 8000 * 2h
        self.assertEqual(quote["venue_fee_cents"], 10000)  # 5000 * 2h
        self.assertEqual(quote["material_fee_cents"], 450)  # 5*50 + 10*20
        self.assertEqual(quote["total_cents"], 26450)

        locked = self.bookings.lock(booking_id, {"idempotency_key": "k-lock-1"})
        self.assertEqual(locked["status"], "LOCKED")
        self.assertIsNotNone(locked["lock_expires_at"])
        self.assertEqual(batch_available(self.store, self.ids["dye_batch_id"]), 95.0)
        self.assertEqual(batch_available(self.store, self.ids["cloth_batch_id"]), 90.0)

        shipped = self.bookings.ship(booking_id, {"idempotency_key": "k-ship-1"})
        self.assertEqual(shipped["status"], "SHIPPED")
        self.assertEqual(len(shipped["shipments"]), 2)
        by_material = {s["material_id"]: s for s in shipped["shipments"]}
        self.assertEqual(by_material["dye"]["quantity"], 5.0)
        self.assertEqual(by_material["cloth"]["quantity"], 10.0)
        self.assertEqual(by_material["dye"]["shipped_at"], by_material["dye"]["eta"])  # 国内批次即达

        for shipment in shipped["shipments"]:
            self.bookings.record_arrival(
                shipment["shipment_id"], {"quantity": shipment["quantity"], "idempotency_key": f"arr-{shipment['shipment_id']}"}
            )
        view = self.bookings.get_booking(booking_id)
        self.assertTrue(all(s["status"] == "ARRIVED" for s in view["shipments"]))

        # 未到开课时间不可签到
        with self.assertRaises(StateError):
            self.bookings.checkin(booking_id)
        self.clock.set(datetime.fromisoformat(SLOT_START))
        checked = self.bookings.checkin(booking_id, {"idempotency_key": "k-chk-1"})
        self.assertEqual(checked["status"], "CHECKED_IN")

        settled = self.bookings.settle(
            booking_id, {"actual_attendance": 8, "idempotency_key": "k-stl-1"}
        )
        self.assertEqual(settled["status"], "SETTLED")
        settlement = settled["settlement"]
        self.assertEqual(settlement["actual_attendance"], 8)
        self.assertEqual(settlement["material_fee_cents"], 360)  # 4*50 + 8*20
        self.assertEqual(settlement["loss_fee_cents"], 0)
        self.assertEqual(settlement["total_cents"], 26360)
        # 国内余料退回库存
        self.assertEqual(batch_available(self.store, self.ids["dye_batch_id"]), 96.0)
        self.assertEqual(batch_available(self.store, self.ids["cloth_batch_id"]), 92.0)
        self.assertEqual(settled["losses"], [])

        events = event_types(settled)
        for expected in (
            "booking_applied",
            "quote_issued",
            "booking_locked",
            "materials_shipped",
            "shipment_arrived",
            "booking_checked_in",
            "booking_settled",
        ):
            self.assertIn(expected, events)

    def test_cross_border_leftover_recorded_as_loss(self) -> None:
        catalog, bookings, clock, store = make_services()
        ids = seed_catalog(catalog, dye_cross_border=True, dye_lead_time_seconds=3600)
        applied = bookings.apply(apply_payload(ids, "k-cb-apply"))
        booking_id = applied["booking_id"]
        bookings.quote(booking_id)
        bookings.lock(booking_id, {"idempotency_key": "k-cb-lock"})
        bookings.ship(booking_id, {"idempotency_key": "k-cb-ship"})
        view = bookings.get_booking(booking_id)
        for shipment in view["shipments"]:
            bookings.record_arrival(shipment["shipment_id"], {"quantity": shipment["quantity"]})
        clock.set(datetime.fromisoformat(SLOT_START))
        bookings.checkin(booking_id)
        settled = bookings.settle(booking_id, {"actual_attendance": 8})
        # 跨境染料余料 1.0 记损耗，国内布料余料 2.0 退回
        losses = {loss["reason"]: loss["quantity"] for loss in settled["losses"]}
        self.assertEqual(losses.get("non_returnable_leftover"), 1.0)
        self.assertEqual(batch_available(store, ids["dye_batch_id"]), 95.0)  # 不退回
        self.assertEqual(batch_available(store, ids["cloth_batch_id"]), 92.0)
        self.assertEqual(settled["settlement"]["loss_fee_cents"], 50)  # 1.0 * 50


if __name__ == "__main__":
    unittest.main()
