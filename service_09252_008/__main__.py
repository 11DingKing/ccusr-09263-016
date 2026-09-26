"""服务入口：``python3 -m service_09252_008 [--port 8080] [--data-dir PATH]``。

运行数据默认落在 ``$SERVICE_09252_008_DATA_DIR`` 或用户状态目录，
绝不写入源码目录。启动时自动执行超时任务恢复。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .application.booking_service import BookingService
from .application.catalog_service import CatalogService
from .application.ports import SystemClock, UuidIdGenerator
from .interfaces.http_api import create_server
from .persistence.sqlite_store import SQLiteStore

ENV_DATA_DIR = "SERVICE_09252_008_DATA_DIR"


def default_data_dir() -> Path:
    configured = os.environ.get(ENV_DATA_DIR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "state" / "service_09252_008"


def build_services(data_dir: Path) -> tuple[CatalogService, BookingService, SQLiteStore]:
    store = SQLiteStore(data_dir / "booking.db")
    clock = SystemClock()
    ids = UuidIdGenerator()
    catalog = CatalogService(store, clock, ids)
    bookings = BookingService(store, clock, ids)
    return catalog, bookings, store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service_09252_008", description="非遗课程交流预约协调服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", type=Path, default=None, help=f"运行数据目录（默认 ${ENV_DATA_DIR} 或用户状态目录）")
    args = parser.parse_args(argv)

    data_dir = args.data_dir or default_data_dir()
    catalog, bookings, store = build_services(data_dir)
    recovered = bookings.recover()  # 重启后恢复超时任务
    if recovered["expired_locks"] or recovered["expired_quotes"]:
        print(f"recovered timeouts: {recovered}")
    server = create_server(args.host, args.port, catalog, bookings)
    print(f"serving on http://{args.host}:{args.port} (data dir: {data_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
