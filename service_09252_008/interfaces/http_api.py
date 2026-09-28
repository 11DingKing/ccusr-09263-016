"""HTTP/JSON 接口边界（仅标准库实现）。

路由覆盖：目录登记、申请、报价、锁定、改期、发运、到货、签到、结算、
取消与超时恢复。幂等键可经 ``Idempotency-Key`` 请求头或载荷字段传入。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..application.booking_service import BookingService
from ..application.catalog_service import (
    COLLECTION_BATCHES,
    COLLECTION_MENTORS,
    COLLECTION_PACKAGES,
    COLLECTION_RESOURCES,
    COLLECTION_WINDOWS,
    CatalogService,
)
from ..domain.errors import (
    BusinessRuleError,
    ConflictError,
    DomainError,
    IdempotencyConflict,
    NotFoundError,
    StateError,
    ValidationError,
)

_ERROR_STATUS = {
    NotFoundError.code: 404,
    ValidationError.code: 400,
    BusinessRuleError.code: 422,
    StateError.code: 409,
    ConflictError.code: 409,
    IdempotencyConflict.code: 409,
}

HandlerFn = Callable[[dict[str, Any], dict[str, str]], Any]


class _Router:
    """极简路由：``(方法, 路径模板) -> 处理函数``，模板段用 ``{name}`` 占位。"""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], HandlerFn]] = []

    def add(self, method: str, template: str, handler: HandlerFn) -> None:
        pattern = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$")
        self._routes.append((method, pattern, handler))

    def match(self, method: str, path: str) -> tuple[HandlerFn, dict[str, str]] | None:
        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            matched = pattern.match(path)
            if matched:
                return handler, matched.groupdict()
        return None


def build_router(catalog: CatalogService, bookings: BookingService) -> _Router:
    router = _Router()

    def with_idempotency_key(payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        key = headers.get("idempotency-key")
        if key and "idempotency_key" not in payload:
            payload = {**payload, "idempotency_key": key}
        return payload

    # 目录登记
    router.add("POST", "/packages", lambda body, hdr: catalog.create_package(body))
    router.add("POST", "/mentors", lambda body, hdr: catalog.create_mentor(body))
    router.add("POST", "/resources", lambda body, hdr: catalog.create_resource(body))
    router.add("POST", "/material-batches", lambda body, hdr: catalog.create_material_batch(body))
    router.add("POST", "/reception-windows", lambda body, hdr: catalog.create_reception_window(body))
    router.add("GET", "/packages", lambda body, hdr: {"items": catalog.list(COLLECTION_PACKAGES)})
    router.add("GET", "/mentors", lambda body, hdr: {"items": catalog.list(COLLECTION_MENTORS)})
    router.add("GET", "/resources", lambda body, hdr: {"items": catalog.list(COLLECTION_RESOURCES)})
    router.add("GET", "/material-batches", lambda body, hdr: {"items": catalog.list(COLLECTION_BATCHES)})
    router.add("GET", "/reception-windows", lambda body, hdr: {"items": catalog.list(COLLECTION_WINDOWS)})
    router.add(
        "GET",
        "/packages/{package_id}",
        lambda body, hdr: catalog.get(COLLECTION_PACKAGES, hdr["__path__"]["package_id"]),
    )

    # 预约流程
    router.add("POST", "/bookings", lambda body, hdr: bookings.apply(with_idempotency_key(body, hdr)))
    router.add("GET", "/bookings/{booking_id}", lambda body, hdr: bookings.get_booking(hdr["__path__"]["booking_id"]))
    router.add(
        "POST",
        "/bookings/{booking_id}/quote",
        lambda body, hdr: bookings.quote(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/lock",
        lambda body, hdr: bookings.lock(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/reschedule",
        lambda body, hdr: bookings.reschedule(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/ship",
        lambda body, hdr: bookings.ship(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/shipments/{shipment_id}/arrivals",
        lambda body, hdr: bookings.record_arrival(hdr["__path__"]["shipment_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/shipments/{shipment_id}/losses",
        lambda body, hdr: bookings.record_shipment_loss(
            hdr["__path__"]["shipment_id"], with_idempotency_key(body, hdr)
        ),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/checkin",
        lambda body, hdr: bookings.checkin(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/settle",
        lambda body, hdr: bookings.settle(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/cancel",
        lambda body, hdr: bookings.cancel(hdr["__path__"]["booking_id"], with_idempotency_key(body, hdr)),
    )
    router.add(
        "POST",
        "/bookings/{booking_id}/cancel/retry",
        lambda body, hdr: bookings.retry_cancel_compensation(hdr["__path__"]["booking_id"]),
    )
    router.add("POST", "/admin/recover", lambda body, hdr: bookings.recover())
    router.add("GET", "/health", lambda body, hdr: {"status": "ok"})
    return router


def make_handler_class(router: _Router) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "HeritageBooking/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
            return

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            matched = router.match(method, path)
            if matched is None:
                self._send_json(404, {"error": "not_found", "message": f"no route for {method} {path}"})
                return
            handler, path_params = matched
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body: dict[str, Any] = {}
                if raw:
                    parsed = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed, dict):
                        raise ValidationError("request body must be a JSON object")
                    body = parsed
                headers = {k.lower(): v for k, v in self.headers.items()}
                headers["__path__"] = path_params  # type: ignore[assignment]
                result = handler(body, headers)
                status = 201 if method == "POST" and path == "/bookings" else 200
                self._send_json(status, result)
            except DomainError as exc:
                self._send_json(_ERROR_STATUS.get(exc.code, 400), exc.to_dict())
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_json", "message": str(exc)})
            except Exception as exc:  # pragma: no cover - 兜底
                self._send_json(500, {"error": "internal_error", "message": str(exc)})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return ApiHandler


def create_server(
    host: str,
    port: int,
    catalog: CatalogService,
    bookings: BookingService,
) -> ThreadingHTTPServer:
    """构建线程化 HTTP 服务（守护线程，随进程退出）。"""
    router = build_router(catalog, bookings)
    server = ThreadingHTTPServer((host, port), make_handler_class(router))
    server.daemon_threads = True
    return server
