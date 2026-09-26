"""部分到货与在途损耗：签到门槛、损耗记录与取消联动。"""
from __future__ import annotations

import unittest
from datetime import datetime

from service_09252_008.domain.errors import BusinessRuleError, StateError
from tests.helpers import SLOT_START, apply_payload, batch_available, make_services, seed_catalog


class PartialArrivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_catalog(self.catalog)
        applied = self.bookings.apply(apply_payload(self.ids, "k-pa-apply"))
        self.booking_id = applied["booking_id"]
        self.bookings.quote(self.booking_id)
        self.bookings.lock(self.booking_id, {"idempotency_key": "k-pa-lock"})
        shipped = self.bookings.ship(self.booking_id, {"idempotency_key": "k-pa-ship"})
        self.shipments = {s["material_id"]: s for s in shipped["shipments"]}

    def test_partial_arrival_blocks_checkin_until_complete(self) -> None:
        dye = self.shipments["dye"]  # 发运 5.0
        view = self.bookings.record_arrival(dye["shipment_id"], {"quantity": 3.0})
        dye_view = next(s for s in view["shipments"] if s["material_id"] == "dye")
        self.assertEqual(dye_view["status"], "PARTIALLY_ARRIVED")
        self.assertEqual(dye_view["arrived_quantity"], 3.0)

        cloth = self.shipments["cloth"]
        self.bookings.record_arrival(cloth["shipment_id"], {"quantity": 10.0})

        # 染料仍在途，不可签到
        self.clock.set(datetime.fromisoformat(SLOT_START))
        with self.assertRaises(StateError) as ctx:
            self.bookings.checkin(self.booking_id)
        self.assertIn("in transit", ctx.exception.message)

        # 剩余 2.0 到货后关闭发运单，可签到
        view = self.bookings.record_arrival(dye["shipment_id"], {"quantity": 2.0})
        dye_view = next(s for s in view["shipments"] if s["material_id"] == "dye")
        self.assertEqual(dye_view["status"], "ARRIVED")
        checked = self.bookings.checkin(self.booking_id)
        self.assertEqual(checked["status"], "CHECKED_IN")

    def test_arrival_overflow_rejected(self) -> None:
        dye = self.shipments["dye"]
        from service_09252_008.domain.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.bookings.record_arrival(dye["shipment_id"], {"quantity": 6.0})

    def test_in_transit_loss_blocks_checkin_and_cancel_records_loss(self) -> None:
        dye = self.shipments["dye"]
        cloth = self.shipments["cloth"]
        self.bookings.record_arrival(dye["shipment_id"], {"quantity": 3.0})
        self.bookings.record_arrival(cloth["shipment_id"], {"quantity": 10.0})
        # 染料剩余 2.0 在途灭失
        view = self.bookings.record_shipment_loss(dye["shipment_id"], {"quantity": 2.0})
        dye_view = next(s for s in view["shipments"] if s["material_id"] == "dye")
        self.assertEqual(dye_view["status"], "CLOSED_WITH_LOSS")
        losses = view["losses"]
        self.assertEqual([(l["reason"], l["quantity"]) for l in losses], [("in_transit_loss", 2.0)])

        # 到货 3.0 < 需要 5.0，签到被拒
        self.clock.set(datetime.fromisoformat(SLOT_START))
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.checkin(self.booking_id)
        self.assertIn("dye", ctx.exception.details["shortages"])

        # 取消：已发运未消耗部分全部记损耗（染料 3.0 + 布料 10.0）
        cancelled = self.bookings.cancel(self.booking_id, {"reason": "材料不足"})
        self.assertEqual(cancelled["status"], "CANCELLED")
        by_reason = {}
        for loss in cancelled["losses"]:
            by_reason.setdefault(loss["reason"], 0.0)
            by_reason[loss["reason"]] += loss["quantity"]
        self.assertEqual(by_reason["in_transit_loss"], 2.0)
        self.assertEqual(by_reason["cancel_after_shipment"], 13.0)
        # 发运部分不退库存，库存保持锁定时的扣减结果
        self.assertEqual(batch_available(self.store, self.ids["dye_batch_id"]), 95.0)
        self.assertEqual(batch_available(self.store, self.ids["cloth_batch_id"]), 90.0)


if __name__ == "__main__":
    unittest.main()
