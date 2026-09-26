"""测试共享辅助：确定性时钟/ID、标准数据 fixtures。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.ports import ManualClock, SequentialIdGenerator
from service_09252_008.persistence.store import InMemoryStore, Store

NOW = datetime(2026, 9, 25, 0, 0, 0, tzinfo=timezone.utc)

# 标准课程时段：2026-10-01 10:00-12:00 Asia/Shanghai == 02:00-04:00 UTC
SLOT_START = "2026-10-01T02:00:00+00:00"
SLOT_END = "2026-10-01T04:00:00+00:00"


def make_services(
    store: Store | None = None,
    *,
    now: datetime = NOW,
    lock_ttl_seconds: int = 1800,
    quote_ttl_seconds: int = 86400,
) -> tuple[CatalogService, BookingService, ManualClock, Store]:
    """构建注入手动时钟与序列 ID 的服务对。"""
    store = store or InMemoryStore()
    clock = ManualClock(now)
    ids = SequentialIdGenerator()
    catalog = CatalogService(store, clock, ids)
    bookings = BookingService(store, clock, ids, lock_ttl_seconds=lock_ttl_seconds, quote_ttl_seconds=quote_ttl_seconds)
    return catalog, bookings, clock, store


def seed_catalog(
    catalog: CatalogService,
    *,
    window_capacity: int = 2,
    window_allowed_safety: int = 2,
    resource_capacity: int = 30,
    resource_safety: int = 2,
    mutex_group: str | None = None,
    mentor_qual_valid_until: str = "2027-01-01T00:00:00+00:00",
    dye_lead_time_seconds: int = 0,
    dye_cross_border: bool = False,
    dye_quantity: float = 100.0,
    cloth_quantity: float = 100.0,
    dye_safety: int = 2,
    window_start: str = "2026-10-01T09:00:00+08:00",
    window_end: str = "2026-10-01T17:00:00+08:00",
) -> dict[str, Any]:
    """登记一套标准目录数据：扎染课程包、导师、工坊、染料/布料批次、接待窗口。"""
    package = catalog.create_package(
        {
            "name": "扎染体验课",
            "craft": "扎染",
            "duration_minutes": 120,
            "max_seats": 20,
            "required_qualifications": ["tie-dye-basic"],
            "materials": [
                {"material_id": "dye", "quantity_per_seat": 0.5},
                {"material_id": "cloth", "quantity_per_seat": 1.0},
            ],
        }
    )
    mentor = catalog.create_mentor(
        {
            "name": "林师傅",
            "home_tz": "Asia/Shanghai",
            "hourly_fee_cents": 8000,
            "qualifications": {"tie-dye-basic": mentor_qual_valid_until},
        }
    )
    resource = catalog.create_resource(
        {
            "name": "染整工坊A",
            "capacity": resource_capacity,
            "safety_rating": resource_safety,
            "mutex_group": mutex_group,
            "tz": "Asia/Shanghai",
            "hourly_fee_cents": 5000,
        }
    )
    dye = catalog.create_material_batch(
        {
            "material_id": "dye",
            "safety": dye_safety,
            "cross_border": dye_cross_border,
            "lead_time_seconds": dye_lead_time_seconds,
            "unit_cost_cents": 50,
            "quantity": dye_quantity,
        }
    )
    cloth = catalog.create_material_batch(
        {
            "material_id": "cloth",
            "safety": 1,
            "cross_border": False,
            "lead_time_seconds": 0,
            "unit_cost_cents": 20,
            "quantity": cloth_quantity,
        }
    )
    window = catalog.create_reception_window(
        {
            "institution": "城南大学",
            "tz": "Asia/Shanghai",
            "start": window_start,
            "end": window_end,
            "capacity": window_capacity,
            "allowed_safety": window_allowed_safety,
        }
    )
    return {
        "package_id": package["package_id"],
        "mentor_id": mentor["mentor_id"],
        "resource_id": resource["resource_id"],
        "dye_batch_id": dye["batch_id"],
        "cloth_batch_id": cloth["batch_id"],
        "window_id": window["window_id"],
    }


def apply_payload(ids: dict[str, Any], key: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "idempotency_key": key,
        "institution": "城北学院",
        "package_id": ids["package_id"],
        "mentor_id": ids["mentor_id"],
        "resource_id": ids["resource_id"],
        "window_id": ids["window_id"],
        "seats": 10,
        "slot_start": SLOT_START,
        "slot_end": SLOT_END,
    }
    payload.update(overrides)
    return payload


def batch_available(store: Store, batch_id: str) -> float:
    record = store.get("material_batches", batch_id)
    assert record is not None
    return record["available_quantity"]


def event_types(view: dict[str, Any]) -> list[str]:
    return [e["type"] for e in view["events"]]
