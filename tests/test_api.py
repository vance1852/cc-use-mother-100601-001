import unittest

from scenic_reservation.api import route
from scenic_reservation.service import ReservationService
from scenic_reservation.storage import Database

DATE = "2026-10-08"
AM = "09:00-12:00"


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ReservationService(self.database)
        self._scenario()

    def tearDown(self):
        self.database.close()

    def _scenario(self):
        status, _ = route(self.service, "POST", "/staff",
                          {"request_id": "admin", "new_actor_id": "a1",
                           "display_name": "管理员", "role": "admin"},
                          {"X-Actor-Id": "bootstrap"})
        self.assertEqual(201, status)
        route(self.service, "POST", "/entrances",
              {"request_id": "gate", "entrance_id": "g1", "name": "北门"},
              {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/capacities",
              {"request_id": "cap", "entrance_id": "g1", "visit_date": DATE,
               "slot": AM, "capacity": 2}, {"X-Actor-Id": "a1"})

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["audit_valid"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/reservations",
                                {"request_id": "x"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_full_reservation_flow_over_http(self):
        status, payload = route(self.service, "POST", "/reservations", {
            "request_id": "r1", "order_id": "o1", "entrance_id": "g1",
            "visit_date": DATE, "slot": AM, "order_type": "individual",
            "quantity": 2, "party_name": "张三",
        })
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        self.assertEqual(2, len(payload["result"]["items"]))

        # 重放返回 200。
        status, replay = route(self.service, "POST", "/reservations", {
            "request_id": "r1", "order_id": "o1", "entrance_id": "g1",
            "visit_date": DATE, "slot": AM, "order_type": "individual",
            "quantity": 2, "party_name": "张三",
        })
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        # 容量已满。
        status, overflow = route(self.service, "POST", "/reservations", {
            "request_id": "r2", "order_id": "o2", "entrance_id": "g1",
            "visit_date": DATE, "slot": AM, "order_type": "group",
            "quantity": 1, "party_name": "李四",
        })
        self.assertEqual(409, status)
        self.assertEqual("capacity_exceeded", overflow["error"])

        status, availability = route(
            self.service, "GET", f"/availability?entrance_id=g1&visit_date={DATE}&slot={AM.replace(':', '%3A')}", None
        )
        self.assertEqual(200, status)
        self.assertEqual(0, availability["usable"])

        status, _ = route(self.service, "POST", "/reservations/confirm",
                          {"request_id": "p1", "order_id": "o1"})
        self.assertEqual(201, status)

        status, ledger_payload = route(
            self.service, "GET", f"/ledger?entrance_id=g1&visit_date={DATE}&slot={AM.replace(':', '%3A')}", None
        )
        self.assertEqual(200, status)
        reasons = {item["reason"] for item in ledger_payload["items"]}
        self.assertEqual({"reserved", "confirmed"}, reasons)

        item_id = ledger_payload["items"][0]["item_id"]
        status, history = route(self.service, "GET", f"/items/{item_id}/history", None)
        self.assertEqual(200, status)
        self.assertEqual("reserved", history["items"][0]["reason"])

        status, _ = route(self.service, "POST", "/reservations/cancel",
                          {"request_id": "c1", "order_id": "o1"})
        self.assertEqual(201, status)
        status, order = route(self.service, "GET", "/orders/o1", None)
        self.assertEqual(200, status)
        self.assertEqual("cancelled", order["state"])

    def test_validation_error_is_structured(self):
        status, payload = route(self.service, "POST", "/capacities",
                                {"request_id": "bad", "entrance_id": "g1",
                                 "visit_date": "2026-13-99", "slot": AM, "capacity": 1},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
