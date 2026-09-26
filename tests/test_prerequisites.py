"""前置培训、容量、安全等级、互斥资源与运输周期等规则校验。"""
from __future__ import annotations

import unittest

from service_09252_008.domain.errors import BusinessRuleError, ValidationError
from tests.helpers import SLOT_END, SLOT_START, apply_payload, make_services, seed_catalog


class PrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()

    def test_missing_qualification_rejected(self) -> None:
        ids = seed_catalog(self.catalog)
        self.catalog.create_mentor(
            {"name": "无证导师", "home_tz": "Asia/Shanghai", "hourly_fee_cents": 100, "qualifications": {}}
        )
        mentor = self.catalog.list("mentors")[1]
        payload = apply_payload(ids, "k-q1", mentor_id=mentor["mentor_id"])
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(payload)
        self.assertEqual(ctx.exception.details["missing"], ["tie-dye-basic"])

    def test_expired_qualification_rejected(self) -> None:
        # 资格在课程结束前一刻失效
        ids = seed_catalog(self.catalog, mentor_qual_valid_until="2026-10-01T03:59:00+00:00")
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(apply_payload(ids, "k-q2"))
        self.assertEqual(ctx.exception.details["expired"], ["tie-dye-basic"])

    def test_seats_exceed_resource_capacity(self) -> None:
        ids = seed_catalog(self.catalog, resource_capacity=5)
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(apply_payload(ids, "k-c1", seats=6))
        self.assertIn("capacity", ctx.exception.message)

    def test_seats_exceed_package_maximum(self) -> None:
        ids = seed_catalog(self.catalog)
        with self.assertRaises(BusinessRuleError):
            self.bookings.apply(apply_payload(ids, "k-c2", seats=21))

    def test_material_safety_above_venue_rejected(self) -> None:
        # 染料为受控材料(3)，场地与窗口仅允许通风级(2)
        ids = seed_catalog(self.catalog, dye_safety=3)
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(apply_payload(ids, "k-s1"))
        self.assertIn("safety", ctx.exception.message)

    def test_cross_border_lead_time_enforced(self) -> None:
        # 接待窗口放宽到 9/26-10/10，便于验证运输周期约束本身
        wide = {"window_start": "2026-09-26T09:00:00+08:00", "window_end": "2026-10-10T17:00:00+08:00"}
        # 跨境批次运输周期 7 天，距开课仅约 6 天 -> 不可行
        ids = seed_catalog(self.catalog, dye_cross_border=True, dye_lead_time_seconds=7 * 86400, **wide)
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(apply_payload(ids, "k-t1"))
        self.assertIn("lead time", ctx.exception.message)
        # 改期到 8 天后则可行
        ids2 = seed_catalog(self.catalog, dye_cross_border=True, dye_lead_time_seconds=7 * 86400, **wide)
        ok = self.bookings.apply(
            apply_payload(
                ids2,
                "k-t2",
                slot_start="2026-10-03T02:00:00+00:00",
                slot_end="2026-10-03T04:00:00+00:00",
            )
        )
        self.assertEqual(ok["status"], "REQUESTED")

    def test_insufficient_stock_rejected(self) -> None:
        ids = seed_catalog(self.catalog, dye_quantity=4.0)  # 需要 5.0
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(apply_payload(ids, "k-m1"))
        self.assertEqual(ctx.exception.details["material_id"], "dye")

    def test_slot_outside_window_rejected(self) -> None:
        ids = seed_catalog(self.catalog)
        with self.assertRaises(BusinessRuleError):
            self.bookings.apply(
                apply_payload(
                    ids,
                    "k-w1",
                    slot_start="2026-10-02T02:00:00+00:00",
                    slot_end="2026-10-02T04:00:00+00:00",
                )
            )

    def test_slot_duration_must_match_package(self) -> None:
        ids = seed_catalog(self.catalog)
        with self.assertRaises(ValidationError):
            self.bookings.apply(
                apply_payload(ids, "k-d1", slot_start=SLOT_START, slot_end="2026-10-01T05:00:00+00:00")
            )

    def test_naive_datetime_rejected(self) -> None:
        ids = seed_catalog(self.catalog)
        with self.assertRaises(ValidationError):
            self.bookings.apply(apply_payload(ids, "k-n1", slot_start="2026-10-01T02:00:00", slot_end=SLOT_END))

    def test_slot_in_past_rejected(self) -> None:
        ids = seed_catalog(self.catalog)
        with self.assertRaises(ValidationError):
            self.bookings.apply(
                apply_payload(
                    ids,
                    "k-p1",
                    slot_start="2026-09-24T02:00:00+00:00",
                    slot_end="2026-09-24T04:00:00+00:00",
                )
            )

    def test_window_capacity_full_goes_waitlist(self) -> None:
        ids = seed_catalog(self.catalog, window_capacity=1)
        first = self.bookings.apply(apply_payload(ids, "k-wl-1"))
        self.assertEqual(first["status"], "REQUESTED")
        second = self.bookings.apply(apply_payload(ids, "k-wl-2"))
        self.assertEqual(second["status"], "WAITLISTED")
        self.assertEqual(second["waitlist_reason"], "window_capacity_full")
        self.assertEqual(second["waitlist_position"], 1)

    def test_mutex_group_blocks_overlapping_resource(self) -> None:
        ids = seed_catalog(self.catalog, window_capacity=5, mutex_group="shared-dye-studio")
        # 同互斥组的第二间工坊
        other = self.catalog.create_resource(
            {
                "name": "染整工坊B",
                "capacity": 30,
                "safety_rating": 2,
                "mutex_group": "shared-dye-studio",
                "tz": "Asia/Shanghai",
                "hourly_fee_cents": 5000,
            }
        )
        first = self.bookings.apply(apply_payload(ids, "k-mx-1"))
        self.bookings.quote(first["booking_id"])
        self.bookings.lock(first["booking_id"], {"idempotency_key": "k-mx-lock"})
        # 同时段、同互斥组的另一资源 -> 候补
        second = self.bookings.apply(apply_payload(ids, "k-mx-2", resource_id=other["resource_id"]))
        self.assertEqual(second["status"], "WAITLISTED")
        self.assertEqual(second["waitlist_reason"], "resource_mutex_blocked")
        # 错峰时段不受影响
        third = self.bookings.apply(
            apply_payload(
                ids,
                "k-mx-3",
                resource_id=other["resource_id"],
                slot_start="2026-10-01T05:00:00+00:00",
                slot_end="2026-10-01T07:00:00+00:00",
            )
        )
        self.assertEqual(third["status"], "REQUESTED")


if __name__ == "__main__":
    unittest.main()
