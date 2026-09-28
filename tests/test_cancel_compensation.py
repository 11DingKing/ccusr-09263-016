"""取消补偿编排：按原因选择动作、动作结果落库、部分失败可重试、重复取消不重复发放。"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.compensation import (
    COMP_ACTION_NOTIFY,
    COMP_ACTION_REFUND,
    COMP_ACTION_VOUCHER,
    LedgerCompensationGateway,
    plan_compensation,
)
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.models import (
    CANCEL_REASON_FORCE_MAJEURE,
    CANCEL_REASON_INSTITUTION_CLOSED,
    CANCEL_REASON_MENTOR_UNAVAILABLE,
    CANCEL_REASON_PLAN_CHANGE,
    COMP_STATUS_FAILED,
    COMP_STATUS_SKIPPED,
    COMP_STATUS_SUCCEEDED,
    Booking,
)
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import NOW, apply_payload, make_services, seed_catalog


def quoted_locked_booking(bookings, ids, key: str):
    applied = bookings.apply(apply_payload(ids, f"{key}-apply"))
    bookings.quote(applied["booking_id"])
    bookings.lock(applied["booking_id"], {"idempotency_key": f"{key}-lock"})
    return applied["booking_id"]


def results_by_type(view: dict) -> dict[str, dict]:
    return {r["action_type"]: r for r in view["compensation_actions"]}


def ledger_by_type(gateway: LedgerCompensationGateway) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in gateway.ledger:
        grouped.setdefault(entry["idempotency_token"].split(":", 1)[0], []).append(entry)
    return grouped


class CompensationPlanTests(unittest.TestCase):
    def test_actions_differ_by_reason(self) -> None:
        catalog, bookings, _, _ = make_services()
        ids = seed_catalog(catalog)
        raw = bookings.apply(apply_payload(ids, "k-plan-apply2"))
        bookings.quote(raw["booking_id"])
        booking = Booking.from_dict(bookings.get_booking(raw["booking_id"]))

        def kinds(reason):
            return [a.action_type for a in plan_compensation(booking, reason)]

        self.assertEqual(kinds(CANCEL_REASON_PLAN_CHANGE), [COMP_ACTION_REFUND])
        self.assertEqual(
            kinds(CANCEL_REASON_INSTITUTION_CLOSED), [COMP_ACTION_REFUND, COMP_ACTION_VOUCHER]
        )
        self.assertEqual(
            kinds(CANCEL_REASON_FORCE_MAJEURE),
            [COMP_ACTION_REFUND, COMP_ACTION_VOUCHER, COMP_ACTION_NOTIFY],
        )
        self.assertEqual(kinds(CANCEL_REASON_MENTOR_UNAVAILABLE), [COMP_ACTION_REFUND, COMP_ACTION_NOTIFY])

    def test_voucher_is_ten_percent_of_quote(self) -> None:
        catalog, bookings, _, _ = make_services()
        ids = seed_catalog(catalog)
        raw = bookings.apply(apply_payload(ids, "k-plan-voucher"))
        quoted = bookings.quote(raw["booking_id"])
        booking = Booking.from_dict(quoted)
        actions = {a.action_type: a for a in plan_compensation(booking, CANCEL_REASON_INSTITUTION_CLOSED)}
        # 报价：导师 16000 + 场地 10000 + 染料 250 + 布料 200 = 26450
        self.assertEqual(actions[COMP_ACTION_REFUND].payload["amount_cents"], 26450)
        self.assertEqual(actions[COMP_ACTION_VOUCHER].payload["face_value_cents"], 2645)


class CompensationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LedgerCompensationGateway()
        self.catalog, self.bookings, self.clock, self.store = make_services(
            compensation_gateway=self.gateway
        )
        self.ids = seed_catalog(self.catalog)

    def test_cancel_delivers_reason_orchestrated_actions(self) -> None:
        booking_id = quoted_locked_booking(self.bookings, self.ids, "k-ok")
        view = self.bookings.cancel(booking_id, {"reason": "院校临时停课"})
        self.assertEqual(view["status"], "CANCELLED")
        self.assertEqual(view["cancel_reason"], CANCEL_REASON_INSTITUTION_CLOSED)
        self.assertNotIn("compensation_warning", view)

        by_type = results_by_type(view)
        self.assertEqual(set(by_type), {COMP_ACTION_REFUND, COMP_ACTION_VOUCHER})
        for result in by_type.values():
            self.assertEqual(result["status"], COMP_STATUS_SUCCEEDED)
            self.assertEqual(result["attempt"], 1)
        self.assertEqual(by_type[COMP_ACTION_REFUND]["payload"]["amount_cents"], 26450)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["payload"]["face_value_cents"], 2645)
        # 外部边界实际发放一次
        delivered = ledger_by_type(self.gateway)
        self.assertEqual(len(delivered[COMP_ACTION_REFUND]), 1)
        self.assertEqual(len(delivered[COMP_ACTION_VOUCHER]), 1)

    def test_second_cancel_does_not_reissue_compensation(self) -> None:
        booking_id = quoted_locked_booking(self.bookings, self.ids, "k-dup")
        first = self.bookings.cancel(
            booking_id, {"reason": "不可抗力", "idempotency_key": "k-dup-cancel"}
        )
        by_type = results_by_type(first)
        self.assertEqual(
            [r["action_type"] for r in first["compensation_actions"]],
            [COMP_ACTION_REFUND, COMP_ACTION_VOUCHER, COMP_ACTION_NOTIFY],
        )
        ledger_before = list(self.gateway.ledger)

        # 重复取消（同一幂等键重放）：返回首次结果，不再向外部边界发放
        replay = self.bookings.cancel(
            booking_id, {"reason": "不可抗力", "idempotency_key": "k-dup-cancel"}
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.gateway.ledger, ledger_before)
        for result in replay["compensation_actions"]:
            self.assertEqual(result["status"], COMP_STATUS_SUCCEEDED)
            self.assertEqual(result["attempt"], 1)

        # 显式重试补偿同样不重复发放已成功的动作
        retried = self.bookings.retry_compensation(booking_id)
        self.assertNotIn("compensation_warning", retried)
        self.assertEqual(len(self.gateway.ledger), len(ledger_before))
        self.assertEqual(len(ledger_by_type(self.gateway)[COMP_ACTION_REFUND]), 1)

    def test_partial_action_failure_then_retry_completes(self) -> None:
        # 补偿券边界不可用：退款成功、补偿券失败、通知跳过
        self.gateway.failing.add(COMP_ACTION_VOUCHER)
        booking_id = quoted_locked_booking(self.bookings, self.ids, "k-partial")
        view = self.bookings.cancel(booking_id, {"reason": CANCEL_REASON_FORCE_MAJEURE})

        # 取消本身不受补偿失败影响：已提交
        self.assertEqual(view["status"], "CANCELLED")
        self.assertIn("compensation_warning", view)
        self.assertEqual(
            view["compensation_warning"]["details"]["failed"], [COMP_ACTION_VOUCHER]
        )
        self.assertEqual(
            view["compensation_warning"]["details"]["skipped"], [COMP_ACTION_NOTIFY]
        )
        by_type = results_by_type(view)
        self.assertEqual(by_type[COMP_ACTION_REFUND]["status"], COMP_STATUS_SUCCEEDED)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["status"], COMP_STATUS_FAILED)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["attempt"], 1)
        self.assertTrue(by_type[COMP_ACTION_VOUCHER]["error"])
        self.assertEqual(by_type[COMP_ACTION_NOTIFY]["status"], COMP_STATUS_SKIPPED)
        self.assertEqual(by_type[COMP_ACTION_NOTIFY]["attempt"], 0)
        # 已成功的退款落袋，失败的补偿券未发放
        delivered = ledger_by_type(self.gateway)
        self.assertEqual(len(delivered[COMP_ACTION_REFUND]), 1)
        self.assertNotIn(COMP_ACTION_VOUCHER, delivered)

        # 边界恢复后续跑：只重试失败/跳过的动作，退款不重复发放
        self.gateway.failing.remove(COMP_ACTION_VOUCHER)
        recovered = self.bookings.retry_compensation(booking_id)
        self.assertNotIn("compensation_warning", recovered)
        by_type = results_by_type(recovered)
        self.assertEqual(by_type[COMP_ACTION_REFUND]["status"], COMP_STATUS_SUCCEEDED)
        self.assertEqual(by_type[COMP_ACTION_REFUND]["attempt"], 1)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["status"], COMP_STATUS_SUCCEEDED)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["attempt"], 2)
        self.assertEqual(by_type[COMP_ACTION_NOTIFY]["status"], COMP_STATUS_SUCCEEDED)
        self.assertEqual(by_type[COMP_ACTION_NOTIFY]["attempt"], 1)
        delivered = ledger_by_type(self.gateway)
        self.assertEqual(len(delivered[COMP_ACTION_REFUND]), 1)
        self.assertEqual(len(delivered[COMP_ACTION_VOUCHER]), 1)
        self.assertEqual(len(delivered[COMP_ACTION_NOTIFY]), 1)

    def test_retry_still_failing_keeps_records_and_preserves_successes(self) -> None:
        self.gateway.failing.add(COMP_ACTION_VOUCHER)
        booking_id = quoted_locked_booking(self.bookings, self.ids, "k-stillfail")
        self.bookings.cancel(booking_id, {"reason": CANCEL_REASON_FORCE_MAJEURE})
        # 边界仍未恢复：补偿券 attempt 递增为 2，通知保持跳过
        again = self.bookings.retry_compensation(booking_id)
        self.assertIn("compensation_warning", again)
        by_type = results_by_type(again)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["status"], COMP_STATUS_FAILED)
        self.assertEqual(by_type[COMP_ACTION_VOUCHER]["attempt"], 2)
        self.assertEqual(by_type[COMP_ACTION_NOTIFY]["status"], COMP_STATUS_SKIPPED)
        # 退款始终只有一次
        self.assertEqual(len(ledger_by_type(self.gateway)[COMP_ACTION_REFUND]), 1)

    def test_cancel_without_quote_refunds_zero(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-noquote"))
        view = self.bookings.cancel(applied["booking_id"], {"reason": CANCEL_REASON_PLAN_CHANGE})
        by_type = results_by_type(view)
        self.assertEqual(set(by_type), {COMP_ACTION_REFUND})
        self.assertEqual(by_type[COMP_ACTION_REFUND]["payload"]["amount_cents"], 0)
        self.assertEqual(by_type[COMP_ACTION_REFUND]["status"], COMP_STATUS_SUCCEEDED)


class CompensationSQLitePersistenceTests(unittest.TestCase):
    def test_action_results_persist_and_retry_skips_succeeded_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)

            # 第一次“进程”：补偿券边界故障，取消后只完成退款
            store = SQLiteStore(db_path)
            catalog = CatalogService(store, clock, UuidIdGenerator())
            gateway = LedgerCompensationGateway(failing={COMP_ACTION_VOUCHER})
            bookings = BookingService(
                store, clock, UuidIdGenerator(), compensation_gateway=gateway
            )
            ids = seed_catalog(catalog)
            booking_id = quoted_locked_booking(bookings, ids, "k-sqlite")
            cancelled = bookings.cancel(booking_id, {"reason": CANCEL_REASON_FORCE_MAJEURE})
            self.assertEqual(cancelled["status"], "CANCELLED")
            # 每个动作结果都在 SQLite 中有独立记录
            rows = store.query("compensation_actions", booking_id=booking_id)
            self.assertEqual(
                {r["action_type"]: r["status"] for r in rows},
                {
                    COMP_ACTION_REFUND: COMP_STATUS_SUCCEEDED,
                    COMP_ACTION_VOUCHER: COMP_STATUS_FAILED,
                    COMP_ACTION_NOTIFY: COMP_STATUS_SKIPPED,
                },
            )
            store.close()

            # “重启”：新服务实例挂载同一数据库，外部边界已恢复（全新台账）
            store2 = SQLiteStore(db_path)
            catalog2 = CatalogService(store2, clock, UuidIdGenerator())
            gateway2 = LedgerCompensationGateway()
            bookings2 = BookingService(
                store2, clock, UuidIdGenerator(), compensation_gateway=gateway2
            )
            recovered = bookings2.retry_compensation(booking_id)
            by_type = results_by_type(recovered)
            self.assertEqual(by_type[COMP_ACTION_REFUND]["status"], COMP_STATUS_SUCCEEDED)
            self.assertEqual(by_type[COMP_ACTION_VOUCHER]["status"], COMP_STATUS_SUCCEEDED)
            self.assertEqual(by_type[COMP_ACTION_NOTIFY]["status"], COMP_STATUS_SUCCEEDED)
            # 重启后基于 SQLite 记录去重：退款已 SUCCEEDED，绝不向新边界再次发放
            new_tokens = [e["idempotency_token"].split(":", 1)[0] for e in gateway2.ledger]
            self.assertNotIn(COMP_ACTION_REFUND, new_tokens)
            self.assertIn(COMP_ACTION_VOUCHER, new_tokens)
            self.assertIn(COMP_ACTION_NOTIFY, new_tokens)
            # 每个动作在 SQLite 中仍只有一行
            rows2 = store2.query("compensation_actions", booking_id=booking_id)
            self.assertEqual(len(rows2), 3)
            store2.close()


if __name__ == "__main__":
    unittest.main()
