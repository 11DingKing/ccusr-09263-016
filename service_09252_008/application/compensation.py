"""取消补偿编排（产品：取消补偿编排）。

取消申请进入补偿流程后，按取消原因选择不同的补偿动作：

- ``plan_change``（申请方计划变更）：仅退款；
- ``institution_closed``（院校停课）/``material_shortage``（材料不足）：退款 + 补偿券；
- ``force_majeure``（不可抗力）：退款 + 补偿券 + 通知；
- ``mentor_unavailable``（导师无法到场）：退款 + 通知；
- 未指明原因：仅退款（保守处理）。

幂等与部分失败约定：

- 每个动作的结果（``SUCCEEDED`` / ``FAILED`` / ``SKIPPED``）单独落一行到
  ``compensation_actions`` 集合（SQLite 持久化），动作结果在各自的事务中提交，
  因而某个动作失败不会回滚已经成功的发放，也不会回滚取消本身；
- 已 ``SUCCEEDED`` 的动作在重复取消/重试时直接跳过，**绝不重复发放**；
- 某动作失败后，其后的动作本次记为 ``SKIPPED``；外部边界恢复后调用
  :meth:`CompensationOrchestrator.run` 即可只重试未成功的动作（``attempt`` 递增）。

外部发放（支付/营销券/通知）通过 :class:`CompensationGateway` 端口接入，
默认实现把发放凭证记入内存台账，生产环境替换为真实边界。
"""
from __future__ import annotations

from typing import Any, Protocol

from ..domain.errors import CompensationDeliveryError
from ..domain.models import (
    CANCEL_REASON_ALIASES,
    CANCEL_REASON_DEFAULT,
    CANCEL_REASON_FORCE_MAJEURE,
    CANCEL_REASON_INSTITUTION_CLOSED,
    CANCEL_REASON_MATERIAL_SHORTAGE,
    CANCEL_REASON_MENTOR_UNAVAILABLE,
    CANCEL_REASON_PLAN_CHANGE,
    CANCEL_REASONS,
    COMP_ACTION_NOTIFY,
    COMP_ACTION_REFUND,
    COMP_ACTION_VOUCHER,
    COMP_STATUS_FAILED,
    COMP_STATUS_SKIPPED,
    COMP_STATUS_SUCCEEDED,
    Booking,
    CompensationAction,
    CompensationActionResult,
    dt_to_str,
)
from ..persistence.store import Store
from .ports import Clock, IdGenerator

COLLECTION_COMPENSATION_ACTIONS = "compensation_actions"
COLLECTION_EVENTS = "events"

#: 补偿券面值：报价金额的 10%（整除）；未报价时发放保底面值
VOUCHER_RATIO_DENOMINATOR = 10
VOUCHER_FALLBACK_CENTS = 1000

#: 动作在补偿计划中的固定先后顺序
COMP_ACTION_ORDER = {COMP_ACTION_REFUND: 0, COMP_ACTION_VOUCHER: 1, COMP_ACTION_NOTIFY: 2}

#: 原因 -> 通知模板与渠道
_NOTIFY_RULES = {
    CANCEL_REASON_FORCE_MAJEURE: ("cancellation_force_majeure", ["institution", "mentor"]),
    CANCEL_REASON_MENTOR_UNAVAILABLE: ("cancellation_mentor_unavailable", ["institution"]),
}

#: 哪些原因除退款外还要发放补偿券
_VOUCHER_REASONS = frozenset(
    {CANCEL_REASON_INSTITUTION_CLOSED, CANCEL_REASON_MATERIAL_SHORTAGE, CANCEL_REASON_FORCE_MAJEURE}
)


def normalize_cancel_reason(reason: str | None) -> str:
    """归一化取消原因：中文别名映射到代码值，未知/缺失归入“未指明”。"""
    if reason is None or not isinstance(reason, str) or not reason.strip():
        return CANCEL_REASON_DEFAULT
    text = reason.strip()
    return CANCEL_REASON_ALIASES.get(text, text if text in CANCEL_REASONS else CANCEL_REASON_DEFAULT)


def _prepaid_total_cents(booking: Booking) -> int:
    """已预收金额：已报价则退报价全额，未报价视为未收款。"""
    return booking.quote.total_cents if booking.quote is not None else 0


def _voucher_face_value(booking: Booking) -> int:
    total = _prepaid_total_cents(booking)
    if total <= 0:
        return VOUCHER_FALLBACK_CENTS
    return max(total // VOUCHER_RATIO_DENOMINATOR, 1)


def plan_compensation(booking: Booking, reason: str) -> list[CompensationAction]:
    """按取消原因编排补偿动作（纯函数，不触碰外部边界）。"""
    total = _prepaid_total_cents(booking)
    actions = [
        CompensationAction(
            COMP_ACTION_REFUND,
            {
                "idempotency_token": f"{COMP_ACTION_REFUND}:{booking.booking_id}",
                "amount_cents": total,
                "currency": "CNY",
                "note": "no prepayment" if total <= 0 else None,
            },
        )
    ]
    if reason in _VOUCHER_REASONS:
        actions.append(
            CompensationAction(
                COMP_ACTION_VOUCHER,
                {
                    "idempotency_token": f"{COMP_ACTION_VOUCHER}:{booking.booking_id}",
                    "face_value_cents": _voucher_face_value(booking),
                },
            )
        )
    notify_rule = _NOTIFY_RULES.get(reason)
    if notify_rule is not None:
        template, channels = notify_rule
        actions.append(
            CompensationAction(
                COMP_ACTION_NOTIFY,
                {
                    "idempotency_token": f"{COMP_ACTION_NOTIFY}:{booking.booking_id}",
                    "template": template,
                    "channels": list(channels),
                },
            )
        )
    return actions


class CompensationGateway(Protocol):
    """补偿外部边界端口：支付退款、营销补偿券、通知。

    实现须对相同 ``idempotency_token`` 做去重；发放失败时抛
    :class:`CompensationDeliveryError`，由编排器记录 ``FAILED`` 并留待重试。
    """

    def refund(
        self,
        *,
        booking_id: str,
        amount_cents: int,
        currency: str,
        reason: str,
        idempotency_token: str,
    ) -> dict[str, Any]:
        ...

    def grant_voucher(
        self,
        *,
        booking_id: str,
        face_value_cents: int,
        reason: str,
        idempotency_token: str,
    ) -> dict[str, Any]:
        ...

    def issue_notification(
        self,
        *,
        booking_id: str,
        channels: list[str],
        template: str,
        reason: str,
        idempotency_token: str,
    ) -> dict[str, Any]:
        ...


class LedgerCompensationGateway:
    """默认边界：退款/补偿券/通知只记入内存台账并返回发放凭证。

    - ``ledger`` 按发放顺序记录全部成功发放，测试可据此断言“只发一次”；
    - ``failing`` 中的动作类型会持续失败，移除后即模拟外部边界恢复；
    - 同一幂等令牌重复发放会被拒绝（编排器之外的第二道防线）。
    """

    def __init__(self, failing: set[str] | None = None) -> None:
        self.ledger: list[dict[str, Any]] = []
        self.failing: set[str] = set(failing or ())
        self._seen: dict[tuple[str, str], dict[str, Any]] = {}
        self._seq = 0

    def _deliver(self, action_type: str, token: str, detail: dict[str, Any]) -> dict[str, Any]:
        if action_type in self.failing:
            raise CompensationDeliveryError(
                f"{action_type} delivery is unavailable",
                details={"idempotency_token": token},
            )
        if (action_type, token) in self._seen:
            raise CompensationDeliveryError(
                f"{action_type} was already delivered for this token",
                details={"idempotency_token": token},
            )
        self._seq += 1
        reference = f"{action_type}-{self._seq:04d}"
        outcome = {"reference": reference, "idempotency_token": token, **detail}
        self._seen[(action_type, token)] = outcome
        self.ledger.append(outcome)
        return outcome

    def refund(
        self,
        *,
        booking_id: str,
        amount_cents: int,
        currency: str,
        reason: str,
        idempotency_token: str,
    ) -> dict[str, Any]:
        return self._deliver(
            COMP_ACTION_REFUND,
            idempotency_token,
            {"booking_id": booking_id, "amount_cents": amount_cents, "currency": currency, "reason": reason},
        )

    def grant_voucher(
        self,
        *,
        booking_id: str,
        face_value_cents: int,
        reason: str,
        idempotency_token: str,
    ) -> dict[str, Any]:
        return self._deliver(
            COMP_ACTION_VOUCHER,
            idempotency_token,
            {"booking_id": booking_id, "face_value_cents": face_value_cents, "reason": reason},
        )

    def issue_notification(
        self,
        *,
        booking_id: str,
        channels: list[str],
        template: str,
        reason: str,
        idempotency_token: str,
    ) -> dict[str, Any]:
        return self._deliver(
            COMP_ACTION_NOTIFY,
            idempotency_token,
            {
                "booking_id": booking_id,
                "channels": list(channels),
                "template": template,
                "reason": reason,
            },
        )


class CompensationOrchestrator:
    """按取消原因驱动补偿动作，逐动作持久化结果，保证不重复发放。"""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        ids: IdGenerator,
        gateway: CompensationGateway | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._gateway = gateway or LedgerCompensationGateway()

    def set_gateway(self, gateway: CompensationGateway) -> None:
        """替换外部边界（测试注入失败/恢复的网关）。"""
        self._gateway = gateway

    @property
    def gateway(self) -> CompensationGateway:
        return self._gateway

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _existing_results(self, booking_id: str) -> dict[str, CompensationActionResult]:
        rows = self._store.query(COLLECTION_COMPENSATION_ACTIONS, booking_id=booking_id)
        return {r["action_type"]: CompensationActionResult.from_dict(r) for r in rows}

    def list_results(self, booking_id: str) -> list[CompensationActionResult]:
        rows = self._store.query(COLLECTION_COMPENSATION_ACTIONS, booking_id=booking_id)
        results = [CompensationActionResult.from_dict(r) for r in rows]
        results.sort(key=lambda r: (COMP_ACTION_ORDER.get(r.action_type, 99), r.executed_at))
        return results

    @staticmethod
    def _result_key(booking_id: str, action_type: str) -> str:
        return f"cmp:{booking_id}:{action_type}"

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def run(self, booking: Booking, reason: str | None = None) -> list[CompensationActionResult]:
        """执行（或续跑）一次取消补偿。

        - 已 ``SUCCEEDED`` 的动作跳过，不再触碰外部边界；
        - ``FAILED`` / ``SKIPPED`` 的动作按计划顺序重试；
        - 仍有动作失败时抛出 :class:`CompensationDeliveryError`，
          每个动作的结果均已独立落库，可稍后再次调用本方法。
        """
        reason = normalize_cancel_reason(reason if reason is not None else booking.cancel_reason)
        plan = plan_compensation(booking, reason)
        existing = self._existing_results(booking.booking_id)
        results: list[CompensationActionResult] = []
        blocked = False
        for action in plan:
            prior = existing.get(action.action_type)
            if prior is not None and prior.status == COMP_STATUS_SUCCEEDED:
                results.append(prior)  # 已发放：幂等跳过，绝不重复调用外部边界
                continue
            if blocked:
                results.append(self._record(booking, reason, action, COMP_STATUS_SKIPPED, prior))
                continue
            try:
                outcome = self._dispatch(booking, reason, action)
            except CompensationDeliveryError as exc:
                results.append(
                    self._record(booking, reason, action, COMP_STATUS_FAILED, prior, error=exc.message)
                )
                blocked = True
            else:
                results.append(
                    self._record(booking, reason, action, COMP_STATUS_SUCCEEDED, prior, outcome=outcome)
                )
        failed = [r.action_type for r in results if r.status == COMP_STATUS_FAILED]
        skipped = [r.action_type for r in results if r.status == COMP_STATUS_SKIPPED]
        if failed:
            raise CompensationDeliveryError(
                "some compensation actions were not delivered",
                details={"failed": failed, "skipped": skipped, "booking_id": booking.booking_id},
            )
        return results

    def _dispatch(self, booking: Booking, reason: str, action: CompensationAction) -> dict[str, Any]:
        payload = action.payload
        if action.action_type == COMP_ACTION_REFUND:
            return self._gateway.refund(
                booking_id=booking.booking_id,
                amount_cents=int(payload["amount_cents"]),
                currency=str(payload["currency"]),
                reason=reason,
                idempotency_token=str(payload["idempotency_token"]),
            )
        if action.action_type == COMP_ACTION_VOUCHER:
            return self._gateway.grant_voucher(
                booking_id=booking.booking_id,
                face_value_cents=int(payload["face_value_cents"]),
                reason=reason,
                idempotency_token=str(payload["idempotency_token"]),
            )
        if action.action_type == COMP_ACTION_NOTIFY:
            return self._gateway.issue_notification(
                booking_id=booking.booking_id,
                channels=list(payload["channels"]),
                template=str(payload["template"]),
                reason=reason,
                idempotency_token=str(payload["idempotency_token"]),
            )
        raise CompensationDeliveryError(
            f"unknown compensation action type: {action.action_type}",
            details={"action_type": action.action_type},
        )

    def _record(
        self,
        booking: Booking,
        reason: str,
        action: CompensationAction,
        status: str,
        prior: CompensationActionResult | None,
        *,
        outcome: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> CompensationActionResult:
        # attempt 记录“实际向外部边界发起的发放次数”：SKIPPED 不计；
        # 续跑沿用同一结果行（同键 upsert），FAILED 后再次发放则递增。
        attempt = prior.attempt if prior is not None else 0
        if status != COMP_STATUS_SKIPPED:
            attempt += 1
        result = CompensationActionResult(
            result_id=self._result_key(booking.booking_id, action.action_type),
            booking_id=booking.booking_id,
            action_type=action.action_type,
            reason=reason,
            status=status,
            payload=dict(action.payload),
            result=dict(outcome or (prior.result if prior is not None else {})),
            error=error,
            attempt=attempt,
            executed_at=self._clock.now(),
        )
        # 动作级独立事务：单个动作失败不影响其他动作结果与取消主流程的持久性
        with self._store.transaction():
            self._store.put(COLLECTION_COMPENSATION_ACTIONS, result.result_id, result.to_dict())
            event = {
                "event_id": self._ids.new_id("evt"),
                "type": f"compensation_{status.lower()}",
                "booking_id": booking.booking_id,
                "payload": {
                    "action_type": action.action_type,
                    "reason": reason,
                    "attempt": attempt,
                    "error": error,
                },
                "created_at": dt_to_str(self._clock.now()),
            }
            self._store.put(COLLECTION_EVENTS, event["event_id"], event)
        return result
