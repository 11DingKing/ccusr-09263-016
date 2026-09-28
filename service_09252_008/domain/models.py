"""领域实体与值对象。

约定：
- 所有时间均为带时区的 ``datetime``，持久化时统一转换为 UTC ISO-8601 字符串；
- 金额一律使用整数“分”，避免浮点误差；
- 数量使用浮点（米/克/件），比较时使用 :data:`QTY_EPS` 容差；
- 实体通过 ``to_dict`` / ``from_dict`` 与持久化层交换纯 JSON 结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any

QTY_EPS = 1e-6

# ---------------------------------------------------------------------------
# 时间辅助
# ---------------------------------------------------------------------------


def dt_to_str(value: datetime) -> str:
    """把带时区时间规范化为 UTC ISO 字符串。"""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def dt_from_str(text: str) -> datetime:
    """解析 ISO 字符串并规范化为 UTC；拒绝朴素时间。"""
    if not isinstance(text, str):
        raise ValueError(f"expected ISO datetime string, got {type(text).__name__}")
    normalized = text.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    value = datetime.fromisoformat(normalized)
    if value.tzinfo is None:
        raise ValueError("datetime string must carry a timezone offset")
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class MaterialSafety(IntEnum):
    """材料安全等级：等级越高对场地要求越苛刻。"""

    GENERAL = 1  # 普通材料：纸张、布料
    VENTILATED = 2  # 需通风：植物染料、大漆
    CONTROLLED = 3  # 受控材料：矿物颜料、溶剂


class BookingStatus(str, Enum):
    REQUESTED = "REQUESTED"  # 已申请，待报价
    QUOTED = "QUOTED"  # 已报价，待锁定
    LOCKED = "LOCKED"  # 资源已锁定（带超时）
    SHIPPED = "SHIPPED"  # 材料已发运
    CHECKED_IN = "CHECKED_IN"  # 已签到开课
    SETTLED = "SETTLED"  # 已结算（终态）
    WAITLISTED = "WAITLISTED"  # 候补排队
    CANCELLED = "CANCELLED"  # 已取消（终态）
    EXPIRED = "EXPIRED"  # 锁定/报价超时释放（终态）


#: 占用接待窗口容量的状态
WINDOW_OCCUPYING_STATUSES = frozenset(
    {
        BookingStatus.REQUESTED,
        BookingStatus.QUOTED,
        BookingStatus.LOCKED,
        BookingStatus.SHIPPED,
        BookingStatus.CHECKED_IN,
    }
)

#: 实际持有工坊资源（互斥判断）的状态
RESOURCE_HOLDING_STATUSES = frozenset(
    {BookingStatus.LOCKED, BookingStatus.SHIPPED, BookingStatus.CHECKED_IN}
)

#: 允许取消的状态
CANCELLABLE_STATUSES = frozenset(
    {
        BookingStatus.REQUESTED,
        BookingStatus.QUOTED,
        BookingStatus.LOCKED,
        BookingStatus.SHIPPED,
        BookingStatus.WAITLISTED,
    }
)


class ShipmentStatus(str, Enum):
    IN_TRANSIT = "IN_TRANSIT"  # 在途
    PARTIALLY_ARRIVED = "PARTIALLY_ARRIVED"  # 部分到货
    ARRIVED = "ARRIVED"  # 全部到货
    CLOSED_WITH_LOSS = "CLOSED_WITH_LOSS"  # 剩余记损耗后关闭


#: 损耗原因
LOSS_CANCEL_AFTER_SHIPMENT = "cancel_after_shipment"  # 发运后取消
LOSS_IN_TRANSIT = "in_transit_loss"  # 在途灭失
LOSS_DAMAGED_IN_USE = "damaged_in_use"  # 课中损坏
LOSS_NON_RETURNABLE_LEFTOVER = "non_returnable_leftover"  # 跨境余料不可退回

#: 取消原因（取消补偿编排据此选择补偿动作）
CANCEL_REASON_PLAN_CHANGE = "plan_change"  # 申请方计划变更
CANCEL_REASON_INSTITUTION_CLOSED = "institution_closed"  # 院校临时停课
CANCEL_REASON_MATERIAL_SHORTAGE = "material_shortage"  # 材料不足无法开课
CANCEL_REASON_FORCE_MAJEURE = "force_majeure"  # 不可抗力
CANCEL_REASON_MENTOR_UNAVAILABLE = "mentor_unavailable"  # 导师无法到场
CANCEL_REASON_DEFAULT = "unspecified"  # 未指明原因

#: 允许的取消原因（未传 reason 时归入“未指明”，不阻断取消）
CANCEL_REASONS = frozenset(
    {
        CANCEL_REASON_PLAN_CHANGE,
        CANCEL_REASON_INSTITUTION_CLOSED,
        CANCEL_REASON_MATERIAL_SHORTAGE,
        CANCEL_REASON_FORCE_MAJEURE,
        CANCEL_REASON_MENTOR_UNAVAILABLE,
        CANCEL_REASON_DEFAULT,
    }
)

#: 取消原因到存储值的别名：兼容中文原因输入，未知值原样保留并按“未指明”编排
CANCEL_REASON_ALIASES = {
    "计划变更": CANCEL_REASON_PLAN_CHANGE,
    "院校临时停课": CANCEL_REASON_INSTITUTION_CLOSED,
    "材料不足": CANCEL_REASON_MATERIAL_SHORTAGE,
    "不可抗力": CANCEL_REASON_FORCE_MAJEURE,
    "导师无法到场": CANCEL_REASON_MENTOR_UNAVAILABLE,
}

#: 补偿动作类型
COMP_ACTION_REFUND = "refund"  # 退还已预收款项
COMP_ACTION_VOUCHER = "compensation_voucher"  # 发放补偿券
COMP_ACTION_NOTIFY = "notify"  # 通知相关方

COMP_ACTION_TYPES = frozenset({COMP_ACTION_REFUND, COMP_ACTION_VOUCHER, COMP_ACTION_NOTIFY})

#: 补偿动作执行结果状态
COMP_STATUS_SUCCEEDED = "SUCCEEDED"
COMP_STATUS_FAILED = "FAILED"
COMP_STATUS_SKIPPED = "SKIPPED"  # 前置发放失败时跳过


# ---------------------------------------------------------------------------
# 课程包 / 导师 / 工坊资源 / 材料批次 / 接待窗口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialRequirement:
    """课程包中单个材料的每席位用量。"""

    material_id: str
    quantity_per_seat: float

    def to_dict(self) -> dict[str, Any]:
        return {"material_id": self.material_id, "quantity_per_seat": self.quantity_per_seat}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MaterialRequirement":
        return cls(material_id=str(data["material_id"]), quantity_per_seat=float(data["quantity_per_seat"]))


@dataclass
class CoursePackage:
    """课程包：一次非遗教学体验的内容定义。"""

    package_id: str
    name: str
    craft: str  # 非遗技艺门类，如扎染、皮影
    duration_minutes: int
    max_seats: int
    required_qualifications: list[str]  # 前置培训（导师须持有且在有效期内）
    materials: list[MaterialRequirement]
    created_at: datetime

    def required_quantity(self, material_id: str, seats: int) -> float:
        for req in self.materials:
            if req.material_id == material_id:
                return req.quantity_per_seat * seats
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "name": self.name,
            "craft": self.craft,
            "duration_minutes": self.duration_minutes,
            "max_seats": self.max_seats,
            "required_qualifications": list(self.required_qualifications),
            "materials": [m.to_dict() for m in self.materials],
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoursePackage":
        return cls(
            package_id=data["package_id"],
            name=data["name"],
            craft=data["craft"],
            duration_minutes=int(data["duration_minutes"]),
            max_seats=int(data["max_seats"]),
            required_qualifications=list(data["required_qualifications"]),
            materials=[MaterialRequirement.from_dict(m) for m in data["materials"]],
            created_at=dt_from_str(data["created_at"]),
        )


@dataclass
class Mentor:
    """导师及其资格（前置培训证书 -> 有效期截止时间）。"""

    mentor_id: str
    name: str
    home_tz: str  # 导师所在时区（IANA）
    hourly_fee_cents: int
    qualifications: dict[str, datetime] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mentor_id": self.mentor_id,
            "name": self.name,
            "home_tz": self.home_tz,
            "hourly_fee_cents": self.hourly_fee_cents,
            "qualifications": {code: dt_to_str(until) for code, until in self.qualifications.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mentor":
        return cls(
            mentor_id=data["mentor_id"],
            name=data["name"],
            home_tz=data["home_tz"],
            hourly_fee_cents=int(data["hourly_fee_cents"]),
            qualifications={code: dt_from_str(until) for code, until in data.get("qualifications", {}).items()},
        )


@dataclass
class WorkshopResource:
    """工坊资源：教室/设备，含容量、安全等级与互斥组。"""

    resource_id: str
    name: str
    capacity: int  # 可容纳席位
    safety_rating: MaterialSafety  # 场地可承接的最高材料安全等级
    mutex_group: str | None  # 互斥组：同组资源同一时段不可重叠使用
    tz: str
    hourly_fee_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "capacity": self.capacity,
            "safety_rating": int(self.safety_rating),
            "mutex_group": self.mutex_group,
            "tz": self.tz,
            "hourly_fee_cents": self.hourly_fee_cents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkshopResource":
        return cls(
            resource_id=data["resource_id"],
            name=data["name"],
            capacity=int(data["capacity"]),
            safety_rating=MaterialSafety(int(data["safety_rating"])),
            mutex_group=data.get("mutex_group"),
            tz=data["tz"],
            hourly_fee_cents=int(data["hourly_fee_cents"]),
        )


@dataclass
class MaterialBatch:
    """材料批次：库存、安全等级与运输周期（跨境批次周期更长）。"""

    batch_id: str
    material_id: str
    safety: MaterialSafety
    cross_border: bool
    lead_time_seconds: int  # 运输周期
    unit_cost_cents: int
    total_quantity: float
    available_quantity: float  # 在库可用（锁定扣减、释放回补）

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "material_id": self.material_id,
            "safety": int(self.safety),
            "cross_border": self.cross_border,
            "lead_time_seconds": self.lead_time_seconds,
            "unit_cost_cents": self.unit_cost_cents,
            "total_quantity": self.total_quantity,
            "available_quantity": self.available_quantity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MaterialBatch":
        return cls(
            batch_id=data["batch_id"],
            material_id=data["material_id"],
            safety=MaterialSafety(int(data["safety"])),
            cross_border=bool(data["cross_border"]),
            lead_time_seconds=int(data["lead_time_seconds"]),
            unit_cost_cents=int(data["unit_cost_cents"]),
            total_quantity=float(data["total_quantity"]),
            available_quantity=float(data["available_quantity"]),
        )


@dataclass
class ReceptionWindow:
    """接待窗口：院校对外开放承接课程的时间段。"""

    window_id: str
    institution: str
    tz: str
    start: datetime
    end: datetime
    capacity: int  # 窗口内可同时进行的课程数
    allowed_safety: MaterialSafety  # 允许入场的最高材料安全等级

    def contains(self, slot_start: datetime, slot_end: datetime) -> bool:
        return self.start <= slot_start and slot_end <= self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "institution": self.institution,
            "tz": self.tz,
            "start": dt_to_str(self.start),
            "end": dt_to_str(self.end),
            "capacity": self.capacity,
            "allowed_safety": int(self.allowed_safety),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReceptionWindow":
        return cls(
            window_id=data["window_id"],
            institution=data["institution"],
            tz=data["tz"],
            start=dt_from_str(data["start"]),
            end=dt_from_str(data["end"]),
            capacity=int(data["capacity"]),
            allowed_safety=MaterialSafety(int(data["allowed_safety"])),
        )


# ---------------------------------------------------------------------------
# 预约及其附属
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedAllocation:
    """材料分配计划：从哪个批次取多少。"""

    batch_id: str
    material_id: str
    quantity: float

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "material_id": self.material_id, "quantity": self.quantity}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlannedAllocation":
        return cls(
            batch_id=data["batch_id"],
            material_id=data["material_id"],
            quantity=float(data["quantity"]),
        )


@dataclass
class Quote:
    """报价单。"""

    quote_id: str
    mentor_fee_cents: int
    venue_fee_cents: int
    material_fee_cents: int
    total_cents: int
    currency: str
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "mentor_fee_cents": self.mentor_fee_cents,
            "venue_fee_cents": self.venue_fee_cents,
            "material_fee_cents": self.material_fee_cents,
            "total_cents": self.total_cents,
            "currency": self.currency,
            "expires_at": dt_to_str(self.expires_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Quote":
        return cls(
            quote_id=data["quote_id"],
            mentor_fee_cents=int(data["mentor_fee_cents"]),
            venue_fee_cents=int(data["venue_fee_cents"]),
            material_fee_cents=int(data["material_fee_cents"]),
            total_cents=int(data["total_cents"]),
            currency=data["currency"],
            expires_at=dt_from_str(data["expires_at"]),
        )


@dataclass
class Booking:
    """预约单。"""

    booking_id: str
    institution: str  # 申请院校
    package_id: str
    mentor_id: str
    resource_id: str
    window_id: str
    seats: int
    slot_start: datetime  # UTC
    slot_end: datetime  # UTC
    status: BookingStatus
    created_at: datetime
    updated_at: datetime
    material_plan: list[PlannedAllocation] = field(default_factory=list)
    quote: Quote | None = None
    lock_expires_at: datetime | None = None
    waitlist_reason: str | None = None
    cancel_reason: str | None = None  # 取消原因（归一化代码值），补偿编排据此选动作
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "institution": self.institution,
            "package_id": self.package_id,
            "mentor_id": self.mentor_id,
            "resource_id": self.resource_id,
            "window_id": self.window_id,
            "seats": self.seats,
            "slot_start": dt_to_str(self.slot_start),
            "slot_end": dt_to_str(self.slot_end),
            "status": self.status.value,
            "created_at": dt_to_str(self.created_at),
            "updated_at": dt_to_str(self.updated_at),
            "material_plan": [p.to_dict() for p in self.material_plan],
            "quote": self.quote.to_dict() if self.quote else None,
            "lock_expires_at": dt_to_str(self.lock_expires_at) if self.lock_expires_at else None,
            "waitlist_reason": self.waitlist_reason,
            "cancel_reason": self.cancel_reason,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Booking":
        return cls(
            booking_id=data["booking_id"],
            institution=data["institution"],
            package_id=data["package_id"],
            mentor_id=data["mentor_id"],
            resource_id=data["resource_id"],
            window_id=data["window_id"],
            seats=int(data["seats"]),
            slot_start=dt_from_str(data["slot_start"]),
            slot_end=dt_from_str(data["slot_end"]),
            status=BookingStatus(data["status"]),
            created_at=dt_from_str(data["created_at"]),
            updated_at=dt_from_str(data["updated_at"]),
            material_plan=[PlannedAllocation.from_dict(p) for p in data.get("material_plan", [])],
            quote=Quote.from_dict(data["quote"]) if data.get("quote") else None,
            lock_expires_at=dt_from_str(data["lock_expires_at"]) if data.get("lock_expires_at") else None,
            waitlist_reason=data.get("waitlist_reason"),
            cancel_reason=data.get("cancel_reason"),
            version=int(data.get("version", 0)),
        )


@dataclass
class MaterialReservation:
    """材料预占台账：锁定建立，发运/到货/结算/取消逐步结转。"""

    reservation_id: str
    booking_id: str
    batch_id: str
    material_id: str
    quantity_reserved: float
    quantity_shipped: float = 0.0
    quantity_arrived: float = 0.0
    quantity_consumed: float = 0.0
    quantity_lost: float = 0.0
    quantity_returned: float = 0.0
    quantity_released: float = 0.0

    @property
    def outstanding_reserved(self) -> float:
        """仍被预占但未发运的数量。"""
        return self.quantity_reserved - self.quantity_shipped - self.quantity_released

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "booking_id": self.booking_id,
            "batch_id": self.batch_id,
            "material_id": self.material_id,
            "quantity_reserved": self.quantity_reserved,
            "quantity_shipped": self.quantity_shipped,
            "quantity_arrived": self.quantity_arrived,
            "quantity_consumed": self.quantity_consumed,
            "quantity_lost": self.quantity_lost,
            "quantity_returned": self.quantity_returned,
            "quantity_released": self.quantity_released,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MaterialReservation":
        return cls(
            reservation_id=data["reservation_id"],
            booking_id=data["booking_id"],
            batch_id=data["batch_id"],
            material_id=data["material_id"],
            quantity_reserved=float(data["quantity_reserved"]),
            quantity_shipped=float(data.get("quantity_shipped", 0.0)),
            quantity_arrived=float(data.get("quantity_arrived", 0.0)),
            quantity_consumed=float(data.get("quantity_consumed", 0.0)),
            quantity_lost=float(data.get("quantity_lost", 0.0)),
            quantity_returned=float(data.get("quantity_returned", 0.0)),
            quantity_released=float(data.get("quantity_released", 0.0)),
        )


@dataclass
class Shipment:
    """发运单：支持分批到货与在途损耗。"""

    shipment_id: str
    booking_id: str
    batch_id: str
    material_id: str
    quantity: float
    shipped_at: datetime
    eta: datetime
    arrived_quantity: float = 0.0
    lost_quantity: float = 0.0
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT

    @property
    def remaining(self) -> float:
        return self.quantity - self.arrived_quantity - self.lost_quantity

    @property
    def closed(self) -> bool:
        return self.status in (ShipmentStatus.ARRIVED, ShipmentStatus.CLOSED_WITH_LOSS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shipment_id": self.shipment_id,
            "booking_id": self.booking_id,
            "batch_id": self.batch_id,
            "material_id": self.material_id,
            "quantity": self.quantity,
            "shipped_at": dt_to_str(self.shipped_at),
            "eta": dt_to_str(self.eta),
            "arrived_quantity": self.arrived_quantity,
            "lost_quantity": self.lost_quantity,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Shipment":
        return cls(
            shipment_id=data["shipment_id"],
            booking_id=data["booking_id"],
            batch_id=data["batch_id"],
            material_id=data["material_id"],
            quantity=float(data["quantity"]),
            shipped_at=dt_from_str(data["shipped_at"]),
            eta=dt_from_str(data["eta"]),
            arrived_quantity=float(data.get("arrived_quantity", 0.0)),
            lost_quantity=float(data.get("lost_quantity", 0.0)),
            status=ShipmentStatus(data["status"]),
        )


@dataclass
class LossRecord:
    """损耗记录。"""

    loss_id: str
    batch_id: str
    material_id: str
    quantity: float
    reason: str
    recorded_at: datetime
    booking_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "loss_id": self.loss_id,
            "batch_id": self.batch_id,
            "material_id": self.material_id,
            "quantity": self.quantity,
            "reason": self.reason,
            "recorded_at": dt_to_str(self.recorded_at),
            "booking_id": self.booking_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LossRecord":
        return cls(
            loss_id=data["loss_id"],
            batch_id=data["batch_id"],
            material_id=data["material_id"],
            quantity=float(data["quantity"]),
            reason=data["reason"],
            recorded_at=dt_from_str(data["recorded_at"]),
            booking_id=data.get("booking_id"),
        )


@dataclass(frozen=True)
class CompensationAction:
    """取消补偿计划中的单个动作声明。"""

    action_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"action_type": self.action_type, "payload": dict(self.payload)}


@dataclass
class CompensationActionResult:
    """补偿动作执行结果：每个动作一行，SQLite 持久化，重试时据此跳过已成功者。"""

    result_id: str
    booking_id: str
    action_type: str
    reason: str  # 触发该补偿的取消原因
    status: str
    payload: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    attempt: int
    executed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "booking_id": self.booking_id,
            "action_type": self.action_type,
            "reason": self.reason,
            "status": self.status,
            "payload": dict(self.payload),
            "result": dict(self.result),
            "error": self.error,
            "attempt": self.attempt,
            "executed_at": dt_to_str(self.executed_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompensationActionResult":
        return cls(
            result_id=data["result_id"],
            booking_id=data["booking_id"],
            action_type=data["action_type"],
            reason=data["reason"],
            status=data["status"],
            payload=dict(data.get("payload", {})),
            result=dict(data.get("result", {})),
            error=data.get("error"),
            attempt=int(data.get("attempt", 1)),
            executed_at=dt_from_str(data["executed_at"]),
        )


@dataclass
class Settlement:
    """结算单。"""

    settlement_id: str
    booking_id: str
    actual_attendance: int
    mentor_fee_cents: int
    venue_fee_cents: int
    material_fee_cents: int
    loss_fee_cents: int
    total_cents: int
    currency: str
    settled_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "booking_id": self.booking_id,
            "actual_attendance": self.actual_attendance,
            "mentor_fee_cents": self.mentor_fee_cents,
            "venue_fee_cents": self.venue_fee_cents,
            "material_fee_cents": self.material_fee_cents,
            "loss_fee_cents": self.loss_fee_cents,
            "total_cents": self.total_cents,
            "currency": self.currency,
            "settled_at": dt_to_str(self.settled_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settlement":
        return cls(
            settlement_id=data["settlement_id"],
            booking_id=data["booking_id"],
            actual_attendance=int(data["actual_attendance"]),
            mentor_fee_cents=int(data["mentor_fee_cents"]),
            venue_fee_cents=int(data["venue_fee_cents"]),
            material_fee_cents=int(data["material_fee_cents"]),
            loss_fee_cents=int(data["loss_fee_cents"]),
            total_cents=int(data["total_cents"]),
            currency=data["currency"],
            settled_at=dt_from_str(data["settled_at"]),
        )


@dataclass
class DomainEvent:
    """领域事件：审计与断言用。"""

    event_id: str
    type: str
    booking_id: str | None
    payload: dict[str, Any]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "booking_id": self.booking_id,
            "payload": self.payload,
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainEvent":
        return cls(
            event_id=data["event_id"],
            type=data["type"],
            booking_id=data.get("booking_id"),
            payload=dict(data.get("payload", {})),
            created_at=dt_from_str(data["created_at"]),
        )
