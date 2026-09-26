"""跨时区场景：窗口/时段/资格有效期在不同时区表达下行为一致。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from service_09252_008.domain.errors import BusinessRuleError
from tests.helpers import apply_payload, make_services, seed_catalog


class TimezoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()

    def test_slot_accepted_in_any_offset_and_normalized_to_utc(self) -> None:
        ids = seed_catalog(self.catalog)  # 窗口 09:00-17:00 Asia/Shanghai
        # 同一物理时刻的三种写法：UTC / 上海 / 纽约（夏令时 UTC-4）
        for key, start, end in (
            ("k-tz-utc", "2026-10-01T02:00:00+00:00", "2026-10-01T04:00:00+00:00"),
            ("k-tz-sh", "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00"),
            ("k-tz-ny", "2026-09-30T22:00:00-04:00", "2026-10-01T00:00:00-04:00"),
        ):
            view = self.bookings.apply(apply_payload(ids, key, slot_start=start, slot_end=end))
            self.assertEqual(view["status"], "REQUESTED")
            self.assertEqual(view["slot_start"], "2026-10-01T02:00:00+00:00")
            self.assertEqual(view["slot_end"], "2026-10-01T04:00:00+00:00")
            self.bookings.cancel(view["booking_id"])

    def test_window_boundary_checked_instantwise_not_datewise(self) -> None:
        # 上海窗口 2026-10-01 09:00-17:00；纽约时间 9-30 21:00 的课在窗口内，
        # 而上海本地 10-02 的课在窗口外（即使 UTC 日期仍是 10-01）。
        ids = seed_catalog(self.catalog)
        inside = self.bookings.apply(
            apply_payload(
                ids,
                "k-tz-in",
                slot_start="2026-09-30T21:00:00-04:00",  # == 2026-10-01 09:00 +08:00
                slot_end="2026-09-30T23:00:00-04:00",
            )
        )
        self.assertEqual(inside["status"], "REQUESTED")
        with self.assertRaises(BusinessRuleError):
            self.bookings.apply(
                apply_payload(
                    ids,
                    "k-tz-out",
                    slot_start="2026-10-02T08:00:00+08:00",  # == 2026-10-02 00:00 UTC，窗口外
                    slot_end="2026-10-02T10:00:00+08:00",
                )
            )

    def test_qualification_validity_compared_across_timezones(self) -> None:
        # 资格有效期至课程结束时刻（上海 12:00 == UTC 04:00）恰好覆盖
        ids = seed_catalog(self.catalog, mentor_qual_valid_until="2026-10-01T12:00:00+08:00")
        ok = self.bookings.apply(apply_payload(ids, "k-tz-qual-ok"))
        self.assertEqual(ok["status"], "REQUESTED")
        # 早一分钟失效则不覆盖
        ids2 = seed_catalog(self.catalog, mentor_qual_valid_until="2026-10-01T11:59:00+08:00")
        with self.assertRaises(BusinessRuleError):
            self.bookings.apply(apply_payload(ids2, "k-tz-qual-expired"))

    def test_shipping_eta_computed_in_utc(self) -> None:
        ids = seed_catalog(self.catalog, dye_cross_border=True, dye_lead_time_seconds=3600)
        applied = self.bookings.apply(apply_payload(ids, "k-tz-ship"))
        self.bookings.quote(applied["booking_id"])
        self.bookings.lock(applied["booking_id"], {"idempotency_key": "k-tz-lock"})
        shipped = self.bookings.ship(applied["booking_id"], {"idempotency_key": "k-tz-ship-2"})
        dye = next(s for s in shipped["shipments"] if s["material_id"] == "dye")
        shipped_at = datetime.fromisoformat(shipped["shipments"][0]["shipped_at"])
        eta = datetime.fromisoformat(dye["eta"])
        self.assertEqual((eta - shipped_at).total_seconds(), 3600)
        self.assertEqual(eta.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
