"""取消补偿编排（产品名：取消补偿编排）。

取消申请进入补偿流程后：

- :class:`CancellationPolicy` 按“取消原因 + 预约当前状态”选择补偿动作集合
  与执行顺序（原因挑选附加动作，状态决定必须执行的基线动作）；
- :class:`CompensationOrchestrator` 逐个执行动作，并把每个动作的结果
  （``RUNNING / SUCCEEDED / FAILED``）写入存储端口——SQLite 后端落盘，
  重启后仍可查；
- 重跑（取消补偿重试）时，已 ``SUCCEEDED`` 的动作直接跳过，因此
  “已发出的补偿不会因重复取消再次发放”；``FAILED`` 的动作允许重试。

动作处理器由应用服务注入（库存回补、损耗、关单、候补晋级、通知等），
本模块只负责编排与结果留痕，不理解库存语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..domain.models import BookingStatus, dt_to_str
from .ports import Clock, IdGenerator

COLLECTION_COMPENSATIONS = "cancellation_compensations"
COLLECTION_NOTIFICATIONS = "cancellation_notifications"

# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------

ACTION_RELEASE_RESERVATIONS = "release_reservations"  # 回补未发运的预占库存
ACTION_WRITE_OFF_SHIPPED = "write_off_shipped"  # 已发运材料记损耗
ACTION_CLOSE_SHIPMENTS = "close_shipments"  # 关闭仍在途的发运单
ACTION_PROMOTE_WAITLIST = "promote_waitlist"  # 按申请先后晋级候补
ACTION_NOTIFY_MENTOR = "notify_mentor"  # 通知导师取消
ACTION_ESCALATE_SAFETY = "escalate_safety"  # 安全事件上报安全负责方

#: 动作执行顺序：先善后库存与单据，再释放候补，最后外发通知
ACTION_ORDER: tuple[str, ...] = (
    ACTION_RELEASE_RESERVATIONS,
    ACTION_WRITE_OFF_SHIPPED,
    ACTION_CLOSE_SHIPMENTS,
    ACTION_PROMOTE_WAITLIST,
    ACTION_NOTIFY_MENTOR,
    ACTION_ESCALATE_SAFETY,
)

# ---------------------------------------------------------------------------
# 取消原因
# ---------------------------------------------------------------------------

REASON_PLAN_CHANGED = "plan_changed"  # 计划变更
REASON_SCHOOL_SUSPENSION = "school_suspension"  # 院校临时停课
REASON_SAFETY_INCIDENT = "safety_incident"  # 材料安全事件
REASON_WAITLIST_ABANDONED = "waitlist_abandoned"  # 候补主动放弃

#: 原因 -> 该原因附加的补偿动作
REASON_ACTIONS: dict[str, tuple[str, ...]] = {
    REASON_PLAN_CHANGED: (ACTION_NOTIFY_MENTOR,),
    REASON_SCHOOL_SUSPENSION: (ACTION_NOTIFY_MENTOR,),
    # 安全事件除通知导师外，还必须上报安全负责方
    REASON_SAFETY_INCIDENT: (ACTION_NOTIFY_MENTOR, ACTION_ESCALATE_SAFETY),
    # 候补放弃只触发候补晋级，不外发通知
    REASON_WAITLIST_ABANDONED: (),
}

#: 预约状态 -> 该状态下必须执行的基线补偿动作（与原因无关）
BASELINE_ACTIONS: dict[BookingStatus, tuple[str, ...]] = {
    BookingStatus.REQUESTED: (ACTION_PROMOTE_WAITLIST,),
    BookingStatus.QUOTED: (ACTION_PROMOTE_WAITLIST,),
    BookingStatus.WAITLISTED: (ACTION_PROMOTE_WAITLIST,),
    BookingStatus.LOCKED: (ACTION_RELEASE_RESERVATIONS, ACTION_PROMOTE_WAITLIST),
    BookingStatus.SHIPPED: (
        ACTION_RELEASE_RESERVATIONS,
        ACTION_WRITE_OFF_SHIPPED,
        ACTION_CLOSE_SHIPMENTS,
        ACTION_PROMOTE_WAITLIST,
    ),
}

# ---------------------------------------------------------------------------
# 动作结果状态
# ---------------------------------------------------------------------------

STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"


@dataclass
class CompensationRecord:
    """单个补偿动作的执行结果（按 ``booking_id + action`` 唯一）。"""

    record_id: str
    booking_id: str
    action: str
    reason: str
    status: str
    attempts: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at_str: str = ""
    updated_at_str: str = ""

    @property
    def key(self) -> str:
        return f"{self.booking_id}:{self.action}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "booking_id": self.booking_id,
            "action": self.action,
            "reason": self.reason,
            "status": self.status,
            "attempts": self.attempts,
            "detail": self.detail,
            "error": self.error,
            "created_at": self.created_at_str,
            "updated_at": self.updated_at_str,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompensationRecord":
        return cls(
            record_id=data["record_id"],
            booking_id=data["booking_id"],
            action=data["action"],
            reason=data["reason"],
            status=data["status"],
            attempts=int(data.get("attempts", 0)),
            detail=dict(data.get("detail", {})),
            error=data.get("error"),
            created_at_str=data.get("created_at", ""),
            updated_at_str=data.get("updated_at", ""),
        )


class CancellationPolicy:
    """按原因与预约状态解析补偿动作清单。"""

    DEFAULT_REASON = REASON_PLAN_CHANGED

    def normalize_reason(self, reason: str | None) -> str:
        if isinstance(reason, str) and reason.strip() in REASON_ACTIONS:
            return reason.strip()
        # 自由文本原因（如历史调用传入的中文说明）按默认原因处理
        return self.DEFAULT_REASON

    def actions_for(self, reason: str, status: BookingStatus) -> list[str]:
        selected = set(BASELINE_ACTIONS.get(status, ()))
        selected.update(REASON_ACTIONS.get(reason, ()))
        return [action for action in ACTION_ORDER if action in selected]


CompensationHandler = Callable[[], dict[str, Any] | None]


class CompensationOrchestrator:
    """执行补偿动作并持久化每个动作的结果。

    每个动作在执行前先落 ``RUNNING``，执行后更新为 ``SUCCEEDED`` 或
    ``FAILED``；单个动作失败不中断后续动作（部分失败），由调用方据
    返回摘要决定如何呈现与重试。调用方须已持有存储事务。
    """

    def __init__(self, store: Any, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self.policy = CancellationPolicy()

    def run(
        self,
        *,
        booking_id: str,
        reason: str,
        status: BookingStatus,
        handlers: dict[str, CompensationHandler],
    ) -> dict[str, Any]:
        actions = self.policy.actions_for(reason, status)
        now = dt_to_str(self._clock.now())
        results: list[dict[str, Any]] = []
        failed: list[str] = []
        for action in actions:
            record = self._load(booking_id, action)
            if record is not None and record.status == STATUS_SUCCEEDED:
                # 已发出的补偿不再执行
                results.append(record.to_dict())
                continue
            if record is None:
                record = CompensationRecord(
                    record_id=self._ids.new_id("cmp"),
                    booking_id=booking_id,
                    action=action,
                    reason=reason,
                    status=STATUS_RUNNING,
                    created_at_str=now,
                    updated_at_str=now,
                )
            handler = handlers.get(action)
            if handler is None:
                record.status = STATUS_FAILED
                record.error = f"no handler registered for action {action}"
                record.attempts += 1
                record.updated_at_str = now
                failed.append(action)
                self._store.put(COLLECTION_COMPENSATIONS, record.key, record.to_dict())
                results.append(record.to_dict())
                continue
            record.status = STATUS_RUNNING
            record.attempts += 1
            record.error = None
            record.updated_at_str = now
            self._store.put(COLLECTION_COMPENSATIONS, record.key, record.to_dict())
            try:
                detail = handler() or {}
            except Exception as exc:  # 单个动作失败：记录并继续后续动作
                record.status = STATUS_FAILED
                record.error = str(exc) or exc.__class__.__name__
                record.updated_at_str = dt_to_str(self._clock.now())
                failed.append(action)
            else:
                record.status = STATUS_SUCCEEDED
                record.detail = dict(detail)
                record.updated_at_str = dt_to_str(self._clock.now())
            self._store.put(COLLECTION_COMPENSATIONS, record.key, record.to_dict())
            results.append(record.to_dict())
        results.sort(key=lambda r: ACTION_ORDER.index(r["action"]))
        return {
            "status": "partial" if failed else "completed",
            "reason": reason,
            "actions": [r["action"] for r in results],
            "failed": failed,
            "results": results,
        }

    def retry_failed(
        self,
        *,
        booking_id: str,
        handlers: dict[str, CompensationHandler],
    ) -> dict[str, Any]:
        """仅重试上次失败的动作；已成功的动作跳过，绝不重复发放。"""
        failed_records = [
            CompensationRecord.from_dict(r)
            for r in self._store.query(COLLECTION_COMPENSATIONS, booking_id=booking_id)
            if r["status"] == STATUS_FAILED
        ]
        failed_records.sort(key=lambda r: ACTION_ORDER.index(r.action))
        now = dt_to_str(self._clock.now())
        results: list[dict[str, Any]] = []
        still_failed: list[str] = []
        for record in failed_records:
            handler = handlers.get(record.action)
            record.status = STATUS_RUNNING
            record.attempts += 1
            record.error = None
            record.updated_at_str = now
            self._store.put(COLLECTION_COMPENSATIONS, record.key, record.to_dict())
            if handler is None:
                record.status = STATUS_FAILED
                record.error = f"no handler registered for action {record.action}"
                record.updated_at_str = dt_to_str(self._clock.now())
                still_failed.append(record.action)
                self._store.put(COLLECTION_COMPENSATIONS, record.key, record.to_dict())
                results.append(record.to_dict())
                continue
            try:
                detail = handler() or {}
            except Exception as exc:
                record.status = STATUS_FAILED
                record.error = str(exc) or exc.__class__.__name__
                record.updated_at_str = dt_to_str(self._clock.now())
                still_failed.append(record.action)
            else:
                record.status = STATUS_SUCCEEDED
                record.detail = dict(detail)
                record.updated_at_str = dt_to_str(self._clock.now())
            self._store.put(COLLECTION_COMPENSATIONS, record.key, record.to_dict())
            results.append(record.to_dict())
        results.sort(key=lambda r: ACTION_ORDER.index(r["action"]))
        return {
            "status": "partial" if still_failed else "completed",
            "actions": [r["action"] for r in results],
            "failed": still_failed,
            "results": results,
        }

    def _load(self, booking_id: str, action: str) -> CompensationRecord | None:
        record = self._store.get(COLLECTION_COMPENSATIONS, f"{booking_id}:{action}")
        return CompensationRecord.from_dict(record) if record else None


def list_compensation_records(store: Any, booking_id: str) -> list[dict[str, Any]]:
    """读取某次取消的全部动作结果，按执行顺序返回。"""
    records = [
        CompensationRecord.from_dict(r)
        for r in store.query(COLLECTION_COMPENSATIONS, booking_id=booking_id)
    ]
    records.sort(key=lambda r: ACTION_ORDER.index(r.action))
    return [r.to_dict() for r in records]


# ---------------------------------------------------------------------------
# 通知端口：导师通知 / 安全上报（测试可替换为故障实现）
# ---------------------------------------------------------------------------


class NotificationPort(Protocol):
    """取消通知外发端口。"""

    def notify_mentor_cancelled(self, booking_id: str, mentor_id: str, reason: str) -> dict[str, Any]:
        ...

    def escalate_safety(self, booking_id: str, reason: str, detail: dict[str, Any]) -> dict[str, Any]:
        ...


class LoggingNotificationPort:
    """默认通知端口：不外联，只把通知留痕到存储（同样随 SQLite 落盘）。"""

    def __init__(self, store: Any, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def _record(self, kind: str, booking_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        notification_id = self._ids.new_id("ntf")
        record = {
            "notification_id": notification_id,
            "kind": kind,
            "booking_id": booking_id,
            "payload": payload,
            "sent_at": dt_to_str(self._clock.now()),
        }
        self._store.put(COLLECTION_NOTIFICATIONS, notification_id, record)
        return record

    def notify_mentor_cancelled(self, booking_id: str, mentor_id: str, reason: str) -> dict[str, Any]:
        return self._record(
            ACTION_NOTIFY_MENTOR,
            booking_id,
            {"mentor_id": mentor_id, "reason": reason},
        )

    def escalate_safety(self, booking_id: str, reason: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._record(ACTION_ESCALATE_SAFETY, booking_id, {"reason": reason, **detail})


def list_notifications(store: Any, booking_id: str) -> list[dict[str, Any]]:
    """读取某次取消外发的通知（验证“补偿不重复发放”）。"""
    return list(store.query(COLLECTION_NOTIFICATIONS, booking_id=booking_id))
