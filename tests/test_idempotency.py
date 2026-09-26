"""幂等键：防止重复占位与重复发运。"""
from __future__ import annotations

import unittest

from service_09252_008.domain.errors import IdempotencyConflict, ValidationError
from tests.helpers import apply_payload, batch_available, make_services, seed_catalog


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_catalog(self.catalog)

    def test_apply_replay_returns_same_booking(self) -> None:
        first = self.bookings.apply(apply_payload(self.ids, "k-idem-apply"))
        replay = self.bookings.apply(apply_payload(self.ids, "k-idem-apply"))
        self.assertEqual(first["booking_id"], replay["booking_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertNotIn("idempotent_replay", first)
        # 只创建了一条预约
        self.assertEqual(len(self.store.query("bookings")), 1)

    def test_same_key_different_payload_conflicts(self) -> None:
        self.bookings.apply(apply_payload(self.ids, "k-idem-x"))
        with self.assertRaises(IdempotencyConflict):
            self.bookings.apply(apply_payload(self.ids, "k-idem-x", seats=12))

    def test_key_scoped_to_endpoint(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-idem-y"))
        # 同一键用于不同端点 -> 冲突而非误重放
        with self.assertRaises(IdempotencyConflict):
            self.bookings.quote(applied["booking_id"], {"idempotency_key": "k-idem-y"})

    def test_apply_requires_idempotency_key(self) -> None:
        payload = apply_payload(self.ids, "k-tmp")
        del payload["idempotency_key"]
        with self.assertRaises(ValidationError):
            self.bookings.apply(payload)

    def test_lock_replay_does_not_double_reserve(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-idem-l1"))
        booking_id = applied["booking_id"]
        self.bookings.quote(booking_id)
        first = self.bookings.lock(booking_id, {"idempotency_key": "k-idem-lock"})
        self.assertEqual(batch_available(self.store, self.ids["dye_batch_id"]), 95.0)
        replay = self.bookings.lock(booking_id, {"idempotency_key": "k-idem-lock"})
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["booking_id"], replay["booking_id"])
        # 库存未被二次扣减
        self.assertEqual(batch_available(self.store, self.ids["dye_batch_id"]), 95.0)
        self.assertEqual(batch_available(self.store, self.ids["cloth_batch_id"]), 90.0)
        self.assertEqual(len(self.store.query("material_reservations")), 2)

    def test_ship_replay_does_not_duplicate_shipments(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-idem-s1"))
        booking_id = applied["booking_id"]
        self.bookings.quote(booking_id)
        self.bookings.lock(booking_id, {"idempotency_key": "k-idem-s-lock"})
        first = self.bookings.ship(booking_id, {"idempotency_key": "k-idem-ship"})
        replay = self.bookings.ship(booking_id, {"idempotency_key": "k-idem-ship"})
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.store.query("shipments")), len(first["shipments"]))


if __name__ == "__main__":
    unittest.main()
