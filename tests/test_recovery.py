"""超时恢复：重启后过期锁定释放库存、过期报价回退、候补按规则晋级。"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import NOW, apply_payload, batch_available, event_types, make_services, seed_catalog


class RecoveryTests(unittest.TestCase):
    def test_expired_lock_recovers_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)

            # 第一次“进程”：容量 1 的窗口，A 锁定，B 候补
            store = SQLiteStore(db_path)
            catalog = CatalogService(store, clock, UuidIdGenerator())
            bookings = BookingService(store, clock, UuidIdGenerator(), lock_ttl_seconds=60)
            ids = seed_catalog(catalog, window_capacity=1)
            first = bookings.apply(apply_payload(ids, "k-rec-a"))
            bookings.quote(first["booking_id"])
            bookings.lock(first["booking_id"], {"idempotency_key": "k-rec-lock-a", "ttl_seconds": 60})
            second = bookings.apply(apply_payload(ids, "k-rec-b"))
            self.assertEqual(second["status"], "WAITLISTED")
            self.assertEqual(batch_available(store, ids["dye_batch_id"]), 95.0)
            store.close()

            # 时钟越过锁定期限，模拟重启：全新服务实例挂载同一数据库
            clock.advance(seconds=120)
            store2 = SQLiteStore(db_path)
            catalog2 = CatalogService(store2, clock, UuidIdGenerator())
            bookings2 = BookingService(store2, clock, UuidIdGenerator(), lock_ttl_seconds=60)
            recovered = bookings2.recover()
            self.assertEqual(recovered["expired_locks"], [first["booking_id"]])

            expired = bookings2.get_booking(first["booking_id"])
            self.assertEqual(expired["status"], "EXPIRED")
            self.assertIn("lock_expired", event_types(expired))
            # 库存回补
            self.assertEqual(batch_available(store2, ids["dye_batch_id"]), 100.0)
            self.assertEqual(batch_available(store2, ids["cloth_batch_id"]), 100.0)
            # 候补按规则晋级
            promoted = bookings2.get_booking(second["booking_id"])
            self.assertEqual(promoted["status"], "REQUESTED")
            self.assertIn("waitlist_promoted", event_types(promoted))
            store2.close()

    def test_expired_quote_returns_to_requested(self) -> None:
        catalog, bookings, clock, store = make_services(quote_ttl_seconds=30)
        ids = seed_catalog(catalog)
        applied = bookings.apply(apply_payload(ids, "k-rec-q"))
        bookings.quote(applied["booking_id"])
        clock.advance(seconds=31)
        recovered = bookings.recover()
        self.assertEqual(recovered["expired_quotes"], [applied["booking_id"]])
        view = bookings.get_booking(applied["booking_id"])
        self.assertEqual(view["status"], "REQUESTED")
        self.assertIsNone(view["quote"])
        self.assertIn("quote_expired", event_types(view))

    def test_active_lock_not_touched(self) -> None:
        catalog, bookings, clock, store = make_services(lock_ttl_seconds=3600)
        ids = seed_catalog(catalog)
        applied = bookings.apply(apply_payload(ids, "k-rec-live"))
        bookings.quote(applied["booking_id"])
        bookings.lock(applied["booking_id"], {"idempotency_key": "k-rec-live-lock"})
        clock.advance(seconds=600)  # 未到期
        recovered = bookings.recover()
        self.assertEqual(recovered["expired_locks"], [])
        self.assertEqual(bookings.get_booking(applied["booking_id"])["status"], "LOCKED")
        self.assertEqual(batch_available(store, ids["dye_batch_id"]), 95.0)


if __name__ == "__main__":
    unittest.main()
