"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import ReservationService
from .storage import Database


def _receipt_status(receipt) -> tuple[int, dict[str, Any]]:
    return 200 if receipt.replayed else 201, {
        "request_id": receipt.request_id,
        "replayed": receipt.replayed,
        "resource_type": receipt.resource_type,
        "resource_id": receipt.resource_id,
        "result": receipt.response,
    }


def route(service: ReservationService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        if method == "POST" and parsed.path == "/staff":
            receipt = service.register_staff(actor_id=actor_id, **body)
            return _receipt_status(receipt)
        if method == "POST" and parsed.path == "/entrances":
            receipt = service.register_entrance(actor_id=actor_id, **body)
            return _receipt_status(receipt)
        if method == "POST" and parsed.path == "/capacities":
            receipt = service.set_capacity(actor_id=actor_id, **body)
            return _receipt_status(receipt)

        if method == "POST" and parsed.path == "/reservations":
            receipt = service.reserve(**body)
            return _receipt_status(receipt)
        if method == "POST" and parsed.path == "/reservations/confirm":
            receipt = service.confirm_payment(**body)
            return _receipt_status(receipt)
        if method == "POST" and parsed.path == "/reservations/cancel":
            receipt = service.cancel_order(**body)
            return _receipt_status(receipt)
        if method == "POST" and parsed.path == "/reservations/reschedule":
            receipt = service.reschedule_order(**body)
            return _receipt_status(receipt)
        if method == "POST" and parsed.path == "/maintenance/expire-due":
            return 200, service.expire_due_reservations()

        if method == "GET" and parsed.path == "/availability":
            result = service.availability(
                query.get("entrance_id", [""])[0],
                query.get("visit_date", [""])[0],
                query.get("slot", [""])[0],
            )
            return 200, result.__dict__
        if method == "GET" and parsed.path.startswith("/orders/"):
            order = service.get_order(parsed.path[len("/orders/"):])
            return 200, {
                "order_id": order.order_id, "entrance_id": order.entrance_id,
                "visit_date": order.visit_date, "slot": order.slot,
                "order_type": order.order_type, "party_name": order.party_name,
                "quantity": order.quantity, "state": order.state,
                "request_id": order.request_id, "created_at": order.created_at,
                "updated_at": order.updated_at, "expires_at": order.expires_at,
                "confirmed_at": order.confirmed_at, "superseded_by": order.superseded_by,
                "items": [item.__dict__ for item in order.items],
            }
        if method == "GET" and parsed.path == "/ledger":
            after = int(query.get("after_sequence", ["0"])[0])
            entries = service.ledger(
                query.get("entrance_id", [""])[0],
                query.get("visit_date", [""])[0],
                query.get("slot", [""])[0],
                after,
            )
            return 200, {"items": [entry.__dict__ for entry in entries]}
        if method == "GET" and parsed.path.startswith("/items/") and parsed.path.endswith("/history"):
            item_id = parsed.path[len("/items/"):-len("/history")]
            entries = service.item_history(item_id)
            return 200, {"items": [entry.__dict__ for entry in entries]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: ReservationService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        if not isinstance(body, dict):
            self._write(400, {"error": "invalid_request", "message": "请求体必须是 JSON 对象"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动景区分时预约服务")
    parser.add_argument("--database", default="reservation.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = ReservationService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
