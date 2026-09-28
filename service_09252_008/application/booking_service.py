"""预约核心服务：申请、报价、锁定、发运、到货、签到、结算、取消与超时恢复。

事务与并发约定：
- 每个用例在单个 ``store.transaction()`` 内完成“读-判-写”，
  由存储层的可重入事务保证原子性，从而支持并发锁定；
- 变更类用例支持幂等键：相同键重放返回首次结果，不同载荷复用键则冲突；
- 所有状态迁移都会追加领域事件，便于审计与测试断言。
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Callable

from ..domain.errors import (
    BookingImmutableError,
    BusinessRuleError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    StateError,
    ValidationError,
)
from ..domain.models import (
    CANCELLABLE_STATUSES,
    LOSS_CANCEL_AFTER_SHIPMENT,
    LOSS_DAMAGED_IN_USE,
    LOSS_IN_TRANSIT,
    LOSS_NON_RETURNABLE_LEFTOVER,
    QTY_EPS,
    RESOURCE_HOLDING_STATUSES,
    WINDOW_OCCUPYING_STATUSES,
    Booking,
    BookingStatus,
    DomainEvent,
    LossRecord,
    MaterialBatch,
    MaterialReservation,
    Mentor,
    PlannedAllocation,
    Quote,
    ReceptionWindow,
    Settlement,
    Shipment,
    ShipmentStatus,
    WorkshopResource,
    dt_to_str,
)
from ..domain.rules import (
    ensure_mentor_qualified,
    ensure_resource_fit,
    ensure_slot_shape,
    ensure_window_fit,
    find_resource_conflict,
    plan_material_allocation,
    safety_ceiling,
)
from ..persistence.store import Store
from .cancellation import (
    ACTION_CLOSE_SHIPMENTS,
    ACTION_ESCALATE_SAFETY,
    ACTION_NOTIFY_MENTOR,
    ACTION_PROMOTE_WAITLIST,
    ACTION_RELEASE_RESERVATIONS,
    ACTION_WRITE_OFF_SHIPPED,
    CompensationOrchestrator,
    LoggingNotificationPort,
    NotificationPort,
    list_compensation_records,
)
from .catalog_service import (
    COLLECTION_BATCHES,
    COLLECTION_MENTORS,
    COLLECTION_PACKAGES,
    COLLECTION_RESOURCES,
    COLLECTION_WINDOWS,
)
from .ports import Clock, IdGenerator

COLLECTION_BOOKINGS = "bookings"
COLLECTION_RESERVATIONS = "material_reservations"
COLLECTION_SHIPMENTS = "shipments"
COLLECTION_LOSSES = "material_losses"
COLLECTION_SETTLEMENTS = "settlements"
COLLECTION_EVENTS = "events"
COLLECTION_IDEMPOTENCY = "idempotency_keys"

DEFAULT_LOCK_TTL_SECONDS = 1800
DEFAULT_QUOTE_TTL_SECONDS = 86400
MIN_LOCK_TTL_SECONDS = 60
MAX_LOCK_TTL_SECONDS = 86400


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class BookingService:
    """预约用例编排。"""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        ids: IdGenerator,
        *,
        lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        quote_ttl_seconds: int = DEFAULT_QUOTE_TTL_SECONDS,
        notification_port: NotificationPort | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._lock_ttl = lock_ttl_seconds
        self._quote_ttl = quote_ttl_seconds
        self._notifications = notification_port or LoggingNotificationPort(store, clock, ids)
        self._orchestrator = CompensationOrchestrator(store, clock, ids)

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, booking_id: str | None, payload: dict[str, Any]) -> None:
        event = DomainEvent(
            event_id=self._ids.new_id("evt"),
            type=event_type,
            booking_id=booking_id,
            payload=payload,
            created_at=self._clock.now(),
        )
        self._store.put(COLLECTION_EVENTS, event.event_id, event.to_dict())

    def _idempotent(
        self,
        endpoint: str,
        key: str | None,
        payload: dict[str, Any],
        fn: Callable[[], dict[str, Any]],
        *,
        required: bool,
    ) -> dict[str, Any]:
        """幂等执行：键命中且载荷一致则重放首次结果。"""
        if key is None:
            if required:
                raise ValidationError("idempotency_key is required for this operation")
            with self._store.transaction():
                return fn()
        fingerprint = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        with self._store.transaction():
            existing = self._store.get(COLLECTION_IDEMPOTENCY, key)
            if existing is not None:
                if existing["endpoint"] != endpoint or existing["request_hash"] != fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different request",
                        details={"key": key, "endpoint": endpoint},
                    )
                return {**existing["response"], "idempotent_replay": True}
            result = fn()
            self._store.put(
                COLLECTION_IDEMPOTENCY,
                key,
                {
                    "key": key,
                    "endpoint": endpoint,
                    "request_hash": fingerprint,
                    "response": result,
                    "created_at": dt_to_str(self._clock.now()),
                },
            )
            return result

    # ------------------------------------------------------------------
    # 读取辅助
    # ------------------------------------------------------------------

    def _load_booking(self, booking_id: str) -> Booking:
        record = self._store.get(COLLECTION_BOOKINGS, booking_id)
        if record is None:
            raise NotFoundError(f"booking not found: {booking_id}", details={"booking_id": booking_id})
        return Booking.from_dict(record)

    def _save_booking(self, booking: Booking) -> None:
        booking.version += 1
        booking.updated_at = self._clock.now()
        self._store.put(COLLECTION_BOOKINGS, booking.booking_id, booking.to_dict())

    def _load_package(self, package_id: str):
        from ..domain.models import CoursePackage

        record = self._store.get(COLLECTION_PACKAGES, package_id)
        if record is None:
            raise NotFoundError(f"package not found: {package_id}", details={"package_id": package_id})
        return CoursePackage.from_dict(record)

    def _load_mentor(self, mentor_id: str) -> Mentor:
        record = self._store.get(COLLECTION_MENTORS, mentor_id)
        if record is None:
            raise NotFoundError(f"mentor not found: {mentor_id}", details={"mentor_id": mentor_id})
        return Mentor.from_dict(record)

    def _load_resource(self, resource_id: str) -> WorkshopResource:
        record = self._store.get(COLLECTION_RESOURCES, resource_id)
        if record is None:
            raise NotFoundError(f"resource not found: {resource_id}", details={"resource_id": resource_id})
        return WorkshopResource.from_dict(record)

    def _load_window(self, window_id: str) -> ReceptionWindow:
        record = self._store.get(COLLECTION_WINDOWS, window_id)
        if record is None:
            raise NotFoundError(f"window not found: {window_id}", details={"window_id": window_id})
        return ReceptionWindow.from_dict(record)

    def _load_batch(self, batch_id: str) -> MaterialBatch:
        record = self._store.get(COLLECTION_BATCHES, batch_id)
        if record is None:
            raise NotFoundError(f"material batch not found: {batch_id}", details={"batch_id": batch_id})
        return MaterialBatch.from_dict(record)

    def _save_batch(self, batch: MaterialBatch) -> None:
        self._store.put(COLLECTION_BATCHES, batch.batch_id, batch.to_dict())

    def _reservations_of(self, booking_id: str) -> list[MaterialReservation]:
        return [
            MaterialReservation.from_dict(r)
            for r in self._store.query(COLLECTION_RESERVATIONS, booking_id=booking_id)
        ]

    def _shipments_of(self, booking_id: str) -> list[Shipment]:
        return [Shipment.from_dict(s) for s in self._store.query(COLLECTION_SHIPMENTS, booking_id=booking_id)]

    def _window_bookings(self, window_id: str) -> list[Booking]:
        return [Booking.from_dict(b) for b in self._store.query(COLLECTION_BOOKINGS, window_id=window_id)]

    def _all_bookings(self) -> list[Booking]:
        return [Booking.from_dict(b) for b in self._store.query(COLLECTION_BOOKINGS)]

    # ------------------------------------------------------------------
    # 申请
    # ------------------------------------------------------------------

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """申请预约：校验前置培训、容量、安全、运输周期并生成预约方案。"""
        key = request.get("idempotency_key")
        return self._idempotent("apply", key, request, lambda: self._apply(request), required=True)

    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..domain.models import dt_from_str

        now = self._clock.now()
        for field in ("package_id", "mentor_id", "resource_id", "window_id"):
            if not isinstance(request.get(field), str) or not request[field].strip():
                raise ValidationError(f"field {field} must be a non-empty string", details={"field": field})
        package = self._load_package(request["package_id"].strip())
        mentor = self._load_mentor(request["mentor_id"].strip())
        resource = self._load_resource(request["resource_id"].strip())
        window = self._load_window(request["window_id"].strip())
        institution = request.get("institution")
        if not isinstance(institution, str) or not institution.strip():
            raise ValidationError("field institution must be a non-empty string")
        seats = request.get("seats")
        if isinstance(seats, bool) or not isinstance(seats, int) or seats < 1:
            raise ValidationError("field seats must be a positive integer")
        if seats > package.max_seats:
            raise BusinessRuleError(
                "seats exceed package maximum",
                details={"max_seats": package.max_seats, "seats": seats},
            )
        try:
            slot_start = dt_from_str(request.get("slot_start"))
            slot_end = dt_from_str(request.get("slot_end"))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid slot: {exc}") from exc
        if slot_start <= now:
            raise ValidationError("slot_start must be in the future")

        ensure_slot_shape(package, slot_start, slot_end)
        ensure_window_fit(window, slot_start, slot_end)
        ensure_mentor_qualified(mentor, package, slot_end)
        ensure_resource_fit(resource, seats)

        batches = [MaterialBatch.from_dict(b) for b in self._store.query(COLLECTION_BATCHES)]
        plan = plan_material_allocation(
            package,
            seats,
            batches,
            now=now,
            slot_start=slot_start,
            max_safety=safety_ceiling(resource, window),
        )

        # 容量与互斥：窗口满或互斥资源被持有时进入候补
        candidate = Booking(
            booking_id=self._ids.new_id("bkg"),
            institution=institution.strip(),
            package_id=package.package_id,
            mentor_id=mentor.mentor_id,
            resource_id=resource.resource_id,
            window_id=window.window_id,
            seats=seats,
            slot_start=slot_start,
            slot_end=slot_end,
            status=BookingStatus.REQUESTED,
            created_at=now,
            updated_at=now,
            material_plan=plan,
        )
        others = [(b, self._load_resource(b.resource_id)) for b in self._window_bookings(window.window_id)]
        active = [b for b, _ in others if b.status in WINDOW_OCCUPYING_STATUSES]
        waitlist_reason: str | None = None
        if len(active) >= window.capacity:
            waitlist_reason = "window_capacity_full"
        elif find_resource_conflict(candidate, resource, others, RESOURCE_HOLDING_STATUSES) is not None:
            waitlist_reason = "resource_mutex_blocked"
        if waitlist_reason:
            candidate.status = BookingStatus.WAITLISTED
            candidate.waitlist_reason = waitlist_reason
        self._store.put(COLLECTION_BOOKINGS, candidate.booking_id, candidate.to_dict())
        self._emit(
            "booking_waitlisted" if waitlist_reason else "booking_applied",
            candidate.booking_id,
            {
                "institution": candidate.institution,
                "seats": seats,
                "waitlist_reason": waitlist_reason,
            },
        )
        return self._booking_view(candidate)

    # ------------------------------------------------------------------
    # 报价
    # ------------------------------------------------------------------

    def quote(self, booking_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        key = request.get("idempotency_key")
        return self._idempotent(
            "quote", key, {"booking_id": booking_id, **request}, lambda: self._quote(booking_id), required=False
        )

    def _quote(self, booking_id: str) -> dict[str, Any]:
        booking = self._load_booking(booking_id)
        if booking.status != BookingStatus.REQUESTED:
            raise StateError(
                "only a REQUESTED booking can be quoted",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        package = self._load_package(booking.package_id)
        mentor = self._load_mentor(booking.mentor_id)
        resource = self._load_resource(booking.resource_id)
        minutes = package.duration_minutes
        mentor_fee = (mentor.hourly_fee_cents * minutes + 59) // 60
        venue_fee = (resource.hourly_fee_cents * minutes + 59) // 60
        material_fee = 0
        for alloc in booking.material_plan:
            batch = self._load_batch(alloc.batch_id)
            material_fee += int(round(batch.unit_cost_cents * alloc.quantity))
        quote = Quote(
            quote_id=self._ids.new_id("quo"),
            mentor_fee_cents=mentor_fee,
            venue_fee_cents=venue_fee,
            material_fee_cents=material_fee,
            total_cents=mentor_fee + venue_fee + material_fee,
            currency="CNY",
            expires_at=self._clock.now() + timedelta(seconds=self._quote_ttl),
        )
        booking.quote = quote
        booking.status = BookingStatus.QUOTED
        self._save_booking(booking)
        self._emit("quote_issued", booking.booking_id, {"total_cents": quote.total_cents})
        return self._booking_view(booking)

    # ------------------------------------------------------------------
    # 锁定（并发敏感，幂等键防重复占位）
    # ------------------------------------------------------------------

    def lock(self, booking_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        key = request.get("idempotency_key")
        return self._idempotent(
            "lock",
            key,
            {"booking_id": booking_id, **request},
            lambda: self._lock(booking_id, request),
            required=True,
        )

    def _lock(self, booking_id: str, request: dict[str, Any]) -> dict[str, Any]:
        now = self._clock.now()
        booking = self._load_booking(booking_id)
        if booking.status != BookingStatus.QUOTED:
            raise StateError(
                "only a QUOTED booking can be locked",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        if booking.quote is None or booking.quote.expires_at <= now:
            raise StateError("quote has expired; request a new quote", details={"booking_id": booking_id})
        ttl = request.get("ttl_seconds", self._lock_ttl)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not (MIN_LOCK_TTL_SECONDS <= ttl <= MAX_LOCK_TTL_SECONDS):
            raise ValidationError(
                "ttl_seconds out of range",
                details={"min": MIN_LOCK_TTL_SECONDS, "max": MAX_LOCK_TTL_SECONDS},
            )

        # 互斥资源最终判定：同事务内复查，保证并发锁定只有一个成功者
        resource = self._load_resource(booking.resource_id)
        others = [
            (b, self._load_resource(b.resource_id))
            for b in self._all_bookings()
            if b.booking_id != booking.booking_id
        ]
        conflict = find_resource_conflict(booking, resource, others, RESOURCE_HOLDING_STATUSES)
        if conflict is not None:
            raise ConflictError(
                "resource is held by a conflicting booking",
                details={"conflict_booking_id": conflict.booking_id, "resource_id": resource.resource_id},
            )

        # 依据当前库存重新生成分配计划并预占
        package = self._load_package(booking.package_id)
        window = self._load_window(booking.window_id)
        batches = [MaterialBatch.from_dict(b) for b in self._store.query(COLLECTION_BATCHES)]
        plan = plan_material_allocation(
            package,
            booking.seats,
            batches,
            now=now,
            slot_start=booking.slot_start,
            max_safety=safety_ceiling(resource, window),
        )
        by_id = {b.batch_id: b for b in batches}
        for alloc in plan:
            batch = by_id[alloc.batch_id]
            batch.available_quantity = round(batch.available_quantity - alloc.quantity, 6)
            self._save_batch(batch)
            reservation = MaterialReservation(
                reservation_id=self._ids.new_id("rsv"),
                booking_id=booking.booking_id,
                batch_id=alloc.batch_id,
                material_id=alloc.material_id,
                quantity_reserved=alloc.quantity,
            )
            self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())

        booking.material_plan = plan
        booking.status = BookingStatus.LOCKED
        booking.lock_expires_at = now + timedelta(seconds=ttl)
        self._save_booking(booking)
        self._emit(
            "booking_locked",
            booking.booking_id,
            {"lock_expires_at": dt_to_str(booking.lock_expires_at)},
        )
        return self._booking_view(booking)

    # ------------------------------------------------------------------
    # 改期（已发运不可移动）
    # ------------------------------------------------------------------

    def reschedule(self, booking_id: str, request: dict[str, Any]) -> dict[str, Any]:
        key = request.get("idempotency_key")
        return self._idempotent(
            "reschedule",
            key,
            {"booking_id": booking_id, **request},
            lambda: self._reschedule(booking_id, request),
            required=False,
        )

    def _reschedule(self, booking_id: str, request: dict[str, Any]) -> dict[str, Any]:
        from ..domain.models import dt_from_str

        now = self._clock.now()
        booking = self._load_booking(booking_id)
        if booking.status in (BookingStatus.SHIPPED, BookingStatus.CHECKED_IN, BookingStatus.SETTLED):
            raise BookingImmutableError(
                "booking cannot be moved after materials have shipped",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.WAITLISTED):
            raise StateError(
                "booking in current status cannot be rescheduled",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        try:
            slot_start = dt_from_str(request.get("slot_start"))
            slot_end = dt_from_str(request.get("slot_end"))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid slot: {exc}") from exc
        if slot_start <= now:
            raise ValidationError("slot_start must be in the future")

        package = self._load_package(booking.package_id)
        mentor = self._load_mentor(booking.mentor_id)
        resource = self._load_resource(booking.resource_id)
        window = self._load_window(booking.window_id)
        ensure_slot_shape(package, slot_start, slot_end)
        ensure_window_fit(window, slot_start, slot_end)
        ensure_mentor_qualified(mentor, package, slot_end)
        batches = [MaterialBatch.from_dict(b) for b in self._store.query(COLLECTION_BATCHES)]
        plan = plan_material_allocation(
            package,
            booking.seats,
            batches,
            now=now,
            slot_start=slot_start,
            max_safety=safety_ceiling(resource, window),
        )

        # 已锁定的先释放原预占，回到待报价重新走流程
        if booking.status == BookingStatus.LOCKED:
            self._release_reservations(booking)
        booking.slot_start = slot_start
        booking.slot_end = slot_end
        booking.material_plan = plan
        booking.quote = None
        booking.lock_expires_at = None
        booking.status = BookingStatus.REQUESTED
        self._save_booking(booking)
        self._emit(
            "booking_rescheduled",
            booking.booking_id,
            {"slot_start": dt_to_str(slot_start), "slot_end": dt_to_str(slot_end)},
        )
        return self._booking_view(booking)

    # ------------------------------------------------------------------
    # 发运与到货
    # ------------------------------------------------------------------

    def ship(self, booking_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        key = request.get("idempotency_key")
        return self._idempotent(
            "ship", key, {"booking_id": booking_id, **request}, lambda: self._ship(booking_id), required=True
        )

    def _ship(self, booking_id: str) -> dict[str, Any]:
        now = self._clock.now()
        booking = self._load_booking(booking_id)
        if booking.status != BookingStatus.LOCKED:
            raise StateError(
                "only a LOCKED booking can be shipped",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        if booking.lock_expires_at is None or booking.lock_expires_at <= now:
            raise StateError("lock has expired; recover timeouts before shipping", details={"booking_id": booking_id})
        shipments: list[Shipment] = []
        for reservation in self._reservations_of(booking_id):
            outstanding = reservation.outstanding_reserved
            if outstanding <= QTY_EPS:
                continue
            batch = self._load_batch(reservation.batch_id)
            shipment = Shipment(
                shipment_id=self._ids.new_id("shp"),
                booking_id=booking_id,
                batch_id=batch.batch_id,
                material_id=batch.material_id,
                quantity=outstanding,
                shipped_at=now,
                eta=now + timedelta(seconds=batch.lead_time_seconds),
            )
            reservation.quantity_shipped = round(reservation.quantity_shipped + outstanding, 6)
            self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())
            self._store.put(COLLECTION_SHIPMENTS, shipment.shipment_id, shipment.to_dict())
            shipments.append(shipment)
        if not shipments:
            raise StateError("nothing to ship for this booking", details={"booking_id": booking_id})
        booking.status = BookingStatus.SHIPPED
        booking.lock_expires_at = None
        self._save_booking(booking)
        self._emit(
            "materials_shipped",
            booking_id,
            {"shipment_ids": [s.shipment_id for s in shipments]},
        )
        return self._booking_view(booking)

    def record_arrival(self, shipment_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """登记到货，支持部分到货。"""
        key = request.get("idempotency_key")
        return self._idempotent(
            "record_arrival",
            key,
            {"shipment_id": shipment_id, **request},
            lambda: self._record_arrival(shipment_id, request),
            required=False,
        )

    def _record_arrival(self, shipment_id: str, request: dict[str, Any]) -> dict[str, Any]:
        record = self._store.get(COLLECTION_SHIPMENTS, shipment_id)
        if record is None:
            raise NotFoundError(f"shipment not found: {shipment_id}", details={"shipment_id": shipment_id})
        shipment = Shipment.from_dict(record)
        if shipment.status not in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.PARTIALLY_ARRIVED):
            raise StateError(
                "shipment is already closed",
                details={"shipment_id": shipment_id, "status": shipment.status.value},
            )
        quantity = request.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or float(quantity) <= 0:
            raise ValidationError("field quantity must be a positive number")
        quantity = float(quantity)
        if quantity > shipment.remaining + QTY_EPS:
            raise ValidationError(
                "arrival quantity exceeds shipment remainder",
                details={"remaining": shipment.remaining, "quantity": quantity},
            )
        shipment.arrived_quantity = round(shipment.arrived_quantity + quantity, 6)
        shipment.status = (
            ShipmentStatus.ARRIVED if shipment.remaining <= QTY_EPS else ShipmentStatus.PARTIALLY_ARRIVED
        )
        self._store.put(COLLECTION_SHIPMENTS, shipment.shipment_id, shipment.to_dict())
        for reservation in self._reservations_of(shipment.booking_id):
            if reservation.batch_id == shipment.batch_id:
                reservation.quantity_arrived = round(reservation.quantity_arrived + quantity, 6)
                self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())
                break
        self._emit(
            "shipment_partially_arrived" if shipment.status == ShipmentStatus.PARTIALLY_ARRIVED else "shipment_arrived",
            shipment.booking_id,
            {"shipment_id": shipment_id, "quantity": quantity},
        )
        return self._booking_view(self._load_booking(shipment.booking_id))

    def record_shipment_loss(self, shipment_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """登记在途损耗；剩余全部灭失时关闭发运单。"""
        key = request.get("idempotency_key")
        return self._idempotent(
            "record_shipment_loss",
            key,
            {"shipment_id": shipment_id, **request},
            lambda: self._record_shipment_loss(shipment_id, request),
            required=False,
        )

    def _record_shipment_loss(self, shipment_id: str, request: dict[str, Any]) -> dict[str, Any]:
        record = self._store.get(COLLECTION_SHIPMENTS, shipment_id)
        if record is None:
            raise NotFoundError(f"shipment not found: {shipment_id}", details={"shipment_id": shipment_id})
        shipment = Shipment.from_dict(record)
        if shipment.status not in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.PARTIALLY_ARRIVED):
            raise StateError(
                "shipment is already closed",
                details={"shipment_id": shipment_id, "status": shipment.status.value},
            )
        quantity = request.get("quantity", shipment.remaining)
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or float(quantity) <= 0:
            raise ValidationError("field quantity must be a positive number")
        quantity = float(quantity)
        if quantity > shipment.remaining + QTY_EPS:
            raise ValidationError(
                "loss quantity exceeds shipment remainder",
                details={"remaining": shipment.remaining, "quantity": quantity},
            )
        shipment.lost_quantity = round(shipment.lost_quantity + quantity, 6)
        if shipment.remaining <= QTY_EPS:
            shipment.status = ShipmentStatus.CLOSED_WITH_LOSS
        self._store.put(COLLECTION_SHIPMENTS, shipment.shipment_id, shipment.to_dict())
        for reservation in self._reservations_of(shipment.booking_id):
            if reservation.batch_id == shipment.batch_id:
                reservation.quantity_lost = round(reservation.quantity_lost + quantity, 6)
                self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())
                break
        self._record_loss(
            booking_id=shipment.booking_id,
            batch_id=shipment.batch_id,
            material_id=shipment.material_id,
            quantity=quantity,
            reason=LOSS_IN_TRANSIT,
        )
        return self._booking_view(self._load_booking(shipment.booking_id))

    # ------------------------------------------------------------------
    # 签到与结算
    # ------------------------------------------------------------------

    def checkin(self, booking_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        key = request.get("idempotency_key")
        return self._idempotent(
            "checkin", key, {"booking_id": booking_id, **request}, lambda: self._checkin(booking_id), required=False
        )

    def _checkin(self, booking_id: str) -> dict[str, Any]:
        now = self._clock.now()
        booking = self._load_booking(booking_id)
        if booking.status != BookingStatus.SHIPPED:
            raise StateError(
                "only a SHIPPED booking can be checked in",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        if now < booking.slot_start:
            raise StateError(
                "check-in is not allowed before the slot starts",
                details={"slot_start": dt_to_str(booking.slot_start), "now": dt_to_str(now)},
            )
        shipments = self._shipments_of(booking_id)
        open_shipments = [s.shipment_id for s in shipments if not s.closed]
        if open_shipments:
            raise StateError(
                "cannot check in while shipments are still in transit",
                details={"open_shipments": open_shipments},
            )
        package = self._load_package(booking.package_id)
        arrived_by_material: dict[str, float] = {}
        for reservation in self._reservations_of(booking_id):
            arrived_by_material[reservation.material_id] = arrived_by_material.get(reservation.material_id, 0.0) + (
                reservation.quantity_arrived
            )
        shortages = {
            req.material_id: package.required_quantity(req.material_id, booking.seats)
            for req in package.materials
            if arrived_by_material.get(req.material_id, 0.0) + QTY_EPS
            < package.required_quantity(req.material_id, booking.seats)
        }
        if shortages:
            raise BusinessRuleError(
                "arrived materials are insufficient for the booked seats",
                details={"shortages": shortages},
            )
        booking.status = BookingStatus.CHECKED_IN
        self._save_booking(booking)
        self._emit("booking_checked_in", booking_id, {})
        return self._booking_view(booking)

    def settle(self, booking_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        key = request.get("idempotency_key")
        return self._idempotent(
            "settle",
            key,
            {"booking_id": booking_id, **request},
            lambda: self._settle(booking_id, request),
            required=False,
        )

    def _settle(self, booking_id: str, request: dict[str, Any]) -> dict[str, Any]:
        now = self._clock.now()
        booking = self._load_booking(booking_id)
        if booking.status != BookingStatus.CHECKED_IN:
            raise StateError(
                "only a CHECKED_IN booking can be settled",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        attendance = request.get("actual_attendance")
        if isinstance(attendance, bool) or not isinstance(attendance, int) or not (0 <= attendance <= booking.seats):
            raise ValidationError(
                "actual_attendance must be an integer between 0 and booked seats",
                details={"seats": booking.seats},
            )
        damaged_raw = request.get("damaged", {})
        if not isinstance(damaged_raw, dict):
            raise ValidationError("field damaged must be an object of material_id -> quantity")
        damaged_remaining = {str(k): float(v) for k, v in damaged_raw.items()}
        for material_id, qty in damaged_remaining.items():
            if qty < 0:
                raise ValidationError("damaged quantities must be non-negative", details={"material_id": material_id})

        package = self._load_package(booking.package_id)
        mentor = self._load_mentor(booking.mentor_id)
        resource = self._load_resource(booking.resource_id)
        ratio = attendance / booking.seats if booking.seats else 0.0
        material_fee = 0
        loss_fee = 0
        for reservation in self._reservations_of(booking_id):
            batch = self._load_batch(reservation.batch_id)
            consumed = round(reservation.quantity_reserved * ratio, 6)
            if consumed > reservation.quantity_arrived + QTY_EPS:
                raise BusinessRuleError(
                    "actual attendance exceeds what arrived materials can serve",
                    details={"material_id": reservation.material_id, "arrived": reservation.quantity_arrived},
                )
            # 同种材料可能跨多个批次，损坏量按批次依次分摊
            damaged_qty = round(
                min(damaged_remaining.get(reservation.material_id, 0.0), reservation.quantity_arrived - consumed),
                6,
            )
            damaged_qty = max(damaged_qty, 0.0)
            damaged_remaining[reservation.material_id] = round(
                damaged_remaining.get(reservation.material_id, 0.0) - damaged_qty, 6
            )
            leftover = round(reservation.quantity_arrived - consumed - damaged_qty, 6)
            if leftover < -QTY_EPS:
                raise ValidationError(
                    "damaged quantity exceeds available leftover",
                    details={"material_id": reservation.material_id},
                )
            leftover = max(leftover, 0.0)
            reservation.quantity_consumed = round(reservation.quantity_consumed + consumed, 6)
            material_fee += int(round(batch.unit_cost_cents * consumed))
            if damaged_qty > QTY_EPS:
                reservation.quantity_lost = round(reservation.quantity_lost + damaged_qty, 6)
                loss_fee += int(round(batch.unit_cost_cents * damaged_qty))
                self._record_loss(
                    booking_id=booking_id,
                    batch_id=batch.batch_id,
                    material_id=batch.material_id,
                    quantity=damaged_qty,
                    reason=LOSS_DAMAGED_IN_USE,
                )
            if leftover > QTY_EPS:
                if batch.cross_border:
                    # 跨境余料退回不经济，记损耗
                    reservation.quantity_lost = round(reservation.quantity_lost + leftover, 6)
                    loss_fee += int(round(batch.unit_cost_cents * leftover))
                    self._record_loss(
                        booking_id=booking_id,
                        batch_id=batch.batch_id,
                        material_id=batch.material_id,
                        quantity=leftover,
                        reason=LOSS_NON_RETURNABLE_LEFTOVER,
                    )
                else:
                    reservation.quantity_returned = round(reservation.quantity_returned + leftover, 6)
                    batch.available_quantity = round(batch.available_quantity + leftover, 6)
                    self._save_batch(batch)
            self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())

        unallocated_damage = {m: q for m, q in damaged_remaining.items() if q > QTY_EPS}
        if unallocated_damage:
            raise ValidationError(
                "damaged quantities reference materials beyond this booking",
                details={"unallocated": unallocated_damage},
            )

        minutes = package.duration_minutes
        mentor_fee = (mentor.hourly_fee_cents * minutes + 59) // 60
        venue_fee = (resource.hourly_fee_cents * minutes + 59) // 60
        settlement = Settlement(
            settlement_id=self._ids.new_id("stl"),
            booking_id=booking_id,
            actual_attendance=attendance,
            mentor_fee_cents=mentor_fee,
            venue_fee_cents=venue_fee,
            material_fee_cents=material_fee,
            loss_fee_cents=loss_fee,
            total_cents=mentor_fee + venue_fee + material_fee + loss_fee,
            currency="CNY",
            settled_at=now,
        )
        self._store.put(COLLECTION_SETTLEMENTS, settlement.settlement_id, settlement.to_dict())
        booking.status = BookingStatus.SETTLED
        self._save_booking(booking)
        self._emit("booking_settled", booking_id, {"total_cents": settlement.total_cents})
        return self._booking_view(booking)

    # ------------------------------------------------------------------
    # 取消与候补释放
    # ------------------------------------------------------------------

    def cancel(self, booking_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        key = request.get("idempotency_key")
        return self._idempotent(
            "cancel",
            key,
            {"booking_id": booking_id, **request},
            lambda: self._cancel(booking_id, request),
            required=False,
        )

    def _cancel(self, booking_id: str, request: dict[str, Any]) -> dict[str, Any]:
        booking = self._load_booking(booking_id)
        if booking.status not in CANCELLABLE_STATUSES:
            raise StateError(
                "booking in current status cannot be cancelled",
                details={"booking_id": booking_id, "status": booking.status.value},
            )
        original_status = booking.status
        raw_reason = request.get("reason")
        reason = self._orchestrator.policy.normalize_reason(raw_reason)
        mentor = self._load_mentor(booking.mentor_id)
        handlers = self._compensation_handlers(booking, mentor.mentor_id, reason)

        booking.status = BookingStatus.CANCELLED
        booking.lock_expires_at = None
        self._save_booking(booking)
        self._emit("booking_cancelled", booking_id, {"reason": reason, "raw_reason": raw_reason})
        # 进入“取消补偿编排”：按原因与原状态选择动作，逐动作留痕；
        # 单个动作失败只记 FAILED，不影响取消落定，可事后重试。
        summary = self._orchestrator.run(
            booking_id=booking_id,
            reason=reason,
            status=original_status,
            handlers=handlers,
        )
        view = self._booking_view(booking)
        view["compensation_summary"] = summary
        return view

    def retry_cancel_compensation(self, booking_id: str) -> dict[str, Any]:
        """重试上次取消中失败的补偿动作；已成功动作跳过，不重复发放。"""
        with self._store.transaction():
            booking = self._load_booking(booking_id)
            if booking.status != BookingStatus.CANCELLED:
                raise StateError(
                    "only a CANCELLED booking can retry compensation",
                    details={"booking_id": booking_id, "status": booking.status.value},
                )
            records = list_compensation_records(self._store, booking_id)
            if not records:
                raise StateError(
                    "no compensation has been recorded for this booking",
                    details={"booking_id": booking_id},
                )
            failed = [r["action"] for r in records if r["status"] == "FAILED"]
            if not failed:
                raise StateError(
                    "no failed compensation action to retry",
                    details={"booking_id": booking_id},
                )
            reason = records[0]["reason"]
            mentor = self._load_mentor(booking.mentor_id)
            handlers = self._compensation_handlers(booking, mentor.mentor_id, reason)
            summary = self._orchestrator.retry_failed(booking_id=booking_id, handlers=handlers)
            self._emit("cancel_compensation_retried", booking_id, {"failed": summary["failed"]})
            view = self._booking_view(booking)
            view["compensation_summary"] = summary
            return view

    def _compensation_handlers(
        self, booking: Booking, mentor_id: str, reason: str
    ) -> dict[str, Callable[[], dict[str, Any] | None]]:
        """构造取消补偿动作处理器；各处理器均天然幂等，可安全重试。"""
        booking_id = booking.booking_id

        def release_reservations() -> dict[str, Any]:
            self._release_reservations(booking)
            return {}

        def write_off_shipped() -> dict[str, Any]:
            return {"losses": self._write_off_shipped_materials(booking)}

        def close_shipments() -> dict[str, Any]:
            return {"closed_shipments": self._close_open_shipments(booking)}

        def promote_waitlist() -> dict[str, Any]:
            return {"promoted": self._promote_waitlist(booking.window_id)}

        def notify_mentor() -> dict[str, Any]:
            return self._notifications.notify_mentor_cancelled(booking_id, mentor_id, reason)

        def escalate_safety() -> dict[str, Any]:
            return self._notifications.escalate_safety(
                booking_id,
                reason,
                {"window_id": booking.window_id, "resource_id": booking.resource_id},
            )

        return {
            ACTION_RELEASE_RESERVATIONS: release_reservations,
            ACTION_WRITE_OFF_SHIPPED: write_off_shipped,
            ACTION_CLOSE_SHIPMENTS: close_shipments,
            ACTION_PROMOTE_WAITLIST: promote_waitlist,
            ACTION_NOTIFY_MENTOR: notify_mentor,
            ACTION_ESCALATE_SAFETY: escalate_safety,
        }

    def _release_reservations(self, booking: Booking) -> None:
        """释放未发运的预占库存。"""
        for reservation in self._reservations_of(booking.booking_id):
            outstanding = reservation.outstanding_reserved
            if outstanding <= QTY_EPS:
                continue
            batch = self._load_batch(reservation.batch_id)
            batch.available_quantity = round(batch.available_quantity + outstanding, 6)
            self._save_batch(batch)
            reservation.quantity_released = round(reservation.quantity_released + outstanding, 6)
            self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())

    def _write_off_shipped_materials(self, booking: Booking) -> list[dict[str, Any]]:
        """发运后取消：已发运（在途+已到货）未核销材料全部记损耗。

        未发运的预占同时回补库存；幂等：已记损耗的数量在重试时计入
        ``quantity_lost``，不会重复核算。
        """
        recorded: list[dict[str, Any]] = []
        for reservation in self._reservations_of(booking.booking_id):
            unshipped = reservation.outstanding_reserved
            if unshipped > QTY_EPS:
                batch = self._load_batch(reservation.batch_id)
                batch.available_quantity = round(batch.available_quantity + unshipped, 6)
                self._save_batch(batch)
                reservation.quantity_released = round(reservation.quantity_released + unshipped, 6)
            shipped_uncounted = round(
                reservation.quantity_shipped - reservation.quantity_lost - reservation.quantity_consumed,
                6,
            )
            if shipped_uncounted > QTY_EPS:
                reservation.quantity_lost = round(reservation.quantity_lost + shipped_uncounted, 6)
                self._record_loss(
                    booking_id=booking.booking_id,
                    batch_id=reservation.batch_id,
                    material_id=reservation.material_id,
                    quantity=shipped_uncounted,
                    reason=LOSS_CANCEL_AFTER_SHIPMENT,
                )
                recorded.append(
                    {"material_id": reservation.material_id, "quantity": round(shipped_uncounted, 6)}
                )
            self._store.put(COLLECTION_RESERVATIONS, reservation.reservation_id, reservation.to_dict())
        return recorded

    def _close_open_shipments(self, booking: Booking) -> list[str]:
        """关闭仍在途的发运单，剩余数量记在途灭失；已关闭者跳过。"""
        closed: list[str] = []
        for shipment in self._shipments_of(booking.booking_id):
            if shipment.closed:
                continue
            shipment.status = ShipmentStatus.CLOSED_WITH_LOSS
            shipment.lost_quantity = round(shipment.lost_quantity + shipment.remaining, 6)
            self._store.put(COLLECTION_SHIPMENTS, shipment.shipment_id, shipment.to_dict())
            closed.append(shipment.shipment_id)
        return closed

    def _promote_waitlist(self, window_id: str) -> list[str]:
        """按申请先后顺序释放候补：容量与互斥均满足者晋级为 REQUESTED。"""
        window = self._load_window(window_id)
        waiting = sorted(
            (b for b in self._window_bookings(window_id) if b.status == BookingStatus.WAITLISTED),
            key=lambda b: (b.created_at, b.booking_id),
        )
        promoted: list[str] = []
        if not waiting:
            return promoted
        for candidate in waiting:
            others = [
                (b, self._load_resource(b.resource_id))
                for b in self._window_bookings(window_id)
                if b.booking_id != candidate.booking_id
            ]
            active = [b for b, _ in others if b.status in WINDOW_OCCUPYING_STATUSES]
            if len(active) >= window.capacity:
                continue
            resource = self._load_resource(candidate.resource_id)
            if find_resource_conflict(candidate, resource, others, RESOURCE_HOLDING_STATUSES) is not None:
                continue
            candidate.status = BookingStatus.REQUESTED
            candidate.waitlist_reason = None
            self._save_booking(candidate)
            self._emit("waitlist_promoted", candidate.booking_id, {"window_id": window_id})
            promoted.append(candidate.booking_id)
        return promoted

    def _record_loss(self, *, booking_id: str | None, batch_id: str, material_id: str, quantity: float, reason: str) -> None:
        loss = LossRecord(
            loss_id=self._ids.new_id("los"),
            booking_id=booking_id,
            batch_id=batch_id,
            material_id=material_id,
            quantity=round(quantity, 6),
            reason=reason,
            recorded_at=self._clock.now(),
        )
        self._store.put(COLLECTION_LOSSES, loss.loss_id, loss.to_dict())
        self._emit(
            "material_loss_recorded",
            booking_id,
            {"loss_id": loss.loss_id, "batch_id": batch_id, "quantity": loss.quantity, "reason": reason},
        )

    # ------------------------------------------------------------------
    # 超时恢复（服务重启后调用）
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """恢复超时任务：过期锁定释放库存并晋级候补，过期报价退回待报价。"""
        now = self._clock.now()
        expired_locks: list[str] = []
        expired_quotes: list[str] = []
        with self._store.transaction():
            affected_windows: set[str] = set()
            for booking in self._all_bookings():
                if (
                    booking.status == BookingStatus.LOCKED
                    and booking.lock_expires_at is not None
                    and booking.lock_expires_at <= now
                ):
                    self._release_reservations(booking)
                    booking.status = BookingStatus.EXPIRED
                    booking.lock_expires_at = None
                    self._save_booking(booking)
                    self._emit("lock_expired", booking.booking_id, {})
                    expired_locks.append(booking.booking_id)
                    affected_windows.add(booking.window_id)
                elif (
                    booking.status == BookingStatus.QUOTED
                    and booking.quote is not None
                    and booking.quote.expires_at <= now
                ):
                    booking.quote = None
                    booking.status = BookingStatus.REQUESTED
                    self._save_booking(booking)
                    self._emit("quote_expired", booking.booking_id, {})
                    expired_quotes.append(booking.booking_id)
            for window_id in affected_windows:
                self._promote_waitlist(window_id)
        return {
            "expired_locks": expired_locks,
            "expired_quotes": expired_quotes,
            "recovered_at": dt_to_str(now),
        }

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------

    def get_booking(self, booking_id: str) -> dict[str, Any]:
        booking = self._load_booking(booking_id)
        return self._booking_view(booking)

    def list_bookings(self, **filters: Any) -> list[dict[str, Any]]:
        return [self._booking_view(Booking.from_dict(b)) for b in self._store.query(COLLECTION_BOOKINGS, **filters)]

    def _booking_view(self, booking: Booking) -> dict[str, Any]:
        view = booking.to_dict()
        view["reservations"] = [r.to_dict() for r in self._reservations_of(booking.booking_id)]
        view["shipments"] = [s.to_dict() for s in self._shipments_of(booking.booking_id)]
        settlements = self._store.query(COLLECTION_SETTLEMENTS, booking_id=booking.booking_id)
        view["settlement"] = settlements[0] if settlements else None
        losses = self._store.query(COLLECTION_LOSSES, booking_id=booking.booking_id)
        view["losses"] = losses
        view["compensation"] = list_compensation_records(self._store, booking.booking_id)
        events = [e for e in self._store.query(COLLECTION_EVENTS, booking_id=booking.booking_id)]
        events.sort(key=lambda e: (e["created_at"], e["event_id"]))
        view["events"] = events
        if booking.status == BookingStatus.WAITLISTED:
            ahead = [
                b
                for b in self._window_bookings(booking.window_id)
                if b.status == BookingStatus.WAITLISTED
                and (b.created_at, b.booking_id) < (booking.created_at, booking.booking_id)
            ]
            view["waitlist_position"] = len(ahead) + 1
        return view
