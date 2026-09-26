"""并发锁定：同一互斥资源同一时段只有一个锁定能成功。"""
from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.errors import ConflictError
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import NOW, apply_payload, batch_available, make_services, seed_catalog

CONCURRENT_BOOKINGS = 6


class ConcurrentLockTests(unittest.TestCase):
    def _race_locks(self, catalog: CatalogService, bookings: BookingService, ids: dict) -> list[str]:
        booking_ids = []
        for i in range(CONCURRENT_BOOKINGS):
            applied = bookings.apply(apply_payload(ids, f"k-conc-apply-{i}"))
            assert applied["status"] == "REQUESTED"
            bookings.quote(applied["booking_id"])
            booking_ids.append(applied["booking_id"])

        def try_lock(booking_id: str) -> str:
            try:
                bookings.lock(booking_id, {"idempotency_key": f"k-conc-lock-{booking_id}"})
                return "locked"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=CONCURRENT_BOOKINGS) as pool:
            return list(pool.map(try_lock, booking_ids))

    def test_only_one_lock_wins_in_memory(self) -> None:
        catalog, bookings, clock, store = make_services()
        ids = seed_catalog(catalog, window_capacity=CONCURRENT_BOOKINGS + 1)
        results = self._race_locks(catalog, bookings, ids)
        self.assertEqual(results.count("locked"), 1)
        self.assertEqual(results.count("conflict"), CONCURRENT_BOOKINGS - 1)
        # 只有胜者的预占生效：染料 100 - 5，布料 100 - 10
        self.assertEqual(batch_available(store, ids["dye_batch_id"]), 95.0)
        self.assertEqual(batch_available(store, ids["cloth_batch_id"]), 90.0)
        locked = [b for b in store.query("bookings") if b["status"] == "LOCKED"]
        self.assertEqual(len(locked), 1)

    def test_only_one_lock_wins_with_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(f"{tmp}/booking.db")
            clock = ManualClock(NOW)
            ids_gen = UuidIdGenerator()
            catalog = CatalogService(store, clock, ids_gen)
            bookings = BookingService(store, clock, ids_gen)
            ids = seed_catalog(catalog, window_capacity=CONCURRENT_BOOKINGS + 1)
            results = self._race_locks(catalog, bookings, ids)
            self.assertEqual(results.count("locked"), 1)
            self.assertEqual(results.count("conflict"), CONCURRENT_BOOKINGS - 1)
            self.assertEqual(batch_available(store, ids["dye_batch_id"]), 95.0)
            store.close()


if __name__ == "__main__":
    unittest.main()
