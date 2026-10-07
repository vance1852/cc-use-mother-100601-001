import threading
import unittest
from datetime import datetime, timedelta, timezone

from scenic_reservation.clock import FixedClock
from scenic_reservation.errors import (CapacityExceededError, ConflictError,
                                       NotFoundError, OrderStateError, PermissionDenied,
                                       ValidationError)
from scenic_reservation.service import ReservationService
from scenic_reservation.storage import Database

DATE = "2026-10-08"
AM = "09:00-12:00"
PM = "13:00-16:00"
TZ = timezone(timedelta(hours=8))


class MovableClock:
    """测试用时钟，可向前拨动。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MovableClock(datetime(2026, 10, 7, 20, 0, tzinfo=TZ))
        self.service = ReservationService(self.database, self.clock)
        self.service.register_staff(request_id="admin", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员", role="admin")
        self.service.register_staff(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="运营员", role="operator")
        self.service.register_entrance(request_id="gate", actor_id="op1",
                                       entrance_id="g1", name="北门")
        self.service.set_capacity(request_id="cap-am", actor_id="op1", entrance_id="g1",
                                  visit_date=DATE, slot=AM, capacity=5)
        self.service.set_capacity(request_id="cap-pm", actor_id="op1", entrance_id="g1",
                                  visit_date=DATE, slot=PM, capacity=10)

    def tearDown(self):
        self.database.close()

    # ----- 容量与共享账本 -----

    def test_individual_and_group_share_one_ledger(self):
        self.service.reserve(request_id="r-ind", order_id="o-ind", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual", quantity=2,
                             party_name="张三")
        self.service.reserve(request_id="r-grp", order_id="o-grp", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="group", quantity=3,
                             party_name="假日团")
        slot = self.service.availability("g1", DATE, AM)
        self.assertEqual((5, 0, 5, 0), (slot.capacity, slot.occupied, slot.reserved, slot.usable))

        rows = self.database.connection.execute(
            "SELECT ticket_type, COUNT(*) AS c FROM ticket_items "
            "WHERE state='held' GROUP BY ticket_type ORDER BY ticket_type"
        ).fetchall()
        self.assertEqual({"group": 3, "individual": 2}, {r["ticket_type"]: r["c"] for r in rows})

    def test_overflow_is_rejected_and_nothing_is_held(self):
        self.service.reserve(request_id="r-a", order_id="o-a", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="group", quantity=4,
                             party_name="A团")
        with self.assertRaises(CapacityExceededError):
            self.service.reserve(request_id="r-b", order_id="o-b", entrance_id="g1",
                                 visit_date=DATE, slot=AM, order_type="individual", quantity=2,
                                 party_name="李四")
        slot = self.service.availability("g1", DATE, AM)
        self.assertEqual(4, slot.reserved)
        with self.assertRaises(NotFoundError):
            self.service.get_order("o-b")

    def test_capacity_cannot_drop_below_occupied(self):
        self.service.reserve(request_id="r-a", order_id="o-a", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual", quantity=3,
                             party_name="王五")
        with self.assertRaises(ValidationError):
            self.service.set_capacity(request_id="cap-shrink", actor_id="op1", entrance_id="g1",
                                      visit_date=DATE, slot=AM, capacity=2)

    # ----- 幂等 -----

    def test_duplicate_reserve_request_does_not_double_deduct(self):
        first = self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                                     visit_date=DATE, slot=AM, order_type="individual",
                                     quantity=2, party_name="赵六")
        second = self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                                      visit_date=DATE, slot=AM, order_type="individual",
                                      quantity=2, party_name="赵六")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        slot = self.service.availability("g1", DATE, AM)
        self.assertEqual(2, slot.reserved)
        self.assertEqual(first.response["items"][0]["item_id"],
                         second.response["items"][0]["item_id"])

    def test_same_request_id_with_changed_payload_conflicts(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=2, party_name="赵六")
        with self.assertRaises(ConflictError):
            self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                                 visit_date=DATE, slot=AM, order_type="individual",
                                 quantity=3, party_name="赵六")

    def test_replay_succeeds_even_when_slot_has_since_filled(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=2, party_name="赵六")
        self.service.reserve(request_id="r2", order_id="o2", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="group",
                             quantity=3, party_name="占满团")
        replay = self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                                      visit_date=DATE, slot=AM, order_type="individual",
                                      quantity=2, party_name="赵六")
        self.assertTrue(replay.replayed)
        self.assertEqual(5, self.service.availability("g1", DATE, AM).reserved)

    # ----- 支付确认 -----

    def test_confirm_payment_marks_occupied_without_extra_deduction(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=2, party_name="钱七")
        receipt = self.service.confirm_payment(request_id="pay1", order_id="o1")
        self.assertFalse(receipt.replayed)
        slot = self.service.availability("g1", DATE, AM)
        self.assertEqual((2, 0), (slot.occupied, slot.reserved))
        ledger = self.service.ledger("g1", DATE, AM)
        holds = [e for e in ledger if e.direction == "hold"]
        marks = [e for e in ledger if e.direction == "mark"]
        self.assertEqual(2, len(holds))
        self.assertEqual(2, len(marks))
        self.assertEqual(2, ledger[-1].balance_after)

    def test_confirm_replay_does_not_transition_twice(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=1, party_name="孙八")
        self.service.confirm_payment(request_id="pay1", order_id="o1")
        replay = self.service.confirm_payment(request_id="pay1", order_id="o1")
        self.assertTrue(replay.replayed)
        marks = [e for e in self.service.ledger("g1", DATE, AM) if e.direction == "mark"]
        self.assertEqual(1, len(marks))

    def test_confirm_expired_order_is_rejected(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=1, party_name="周九", hold_seconds=60)
        self.clock.advance(61)
        self.service.expire_due_reservations()
        with self.assertRaises(OrderStateError):
            self.service.confirm_payment(request_id="pay-late", order_id="o1")

    # ----- 过期释放（时钟驱动） -----

    def test_expiry_frees_quota_using_injected_clock(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=2, party_name="吴十", hold_seconds=300)
        self.clock.advance(299)
        self.assertEqual(2, self.service.availability("g1", DATE, AM).reserved)
        self.clock.advance(2)
        result = self.service.expire_due_reservations()
        self.assertEqual(["o1"], result["expired_orders"])
        slot = self.service.availability("g1", DATE, AM)
        self.assertEqual(0, slot.reserved)
        self.assertEqual(5, slot.usable)
        self.assertEqual("expired", self.service.get_order("o1").state)
        # 再次执行没有副作用。
        self.assertEqual(0, self.service.expire_due_reservations()["count"])

    def test_expiry_survives_restart_with_new_clock(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            service = ReservationService(database, FixedClock(datetime(2026, 10, 7, 20, 0, tzinfo=TZ)))
            service.register_staff(request_id="admin", actor_id="bootstrap",
                                   new_actor_id="a1", display_name="管理员", role="admin")
            service.register_entrance(request_id="gate", actor_id="a1",
                                      entrance_id="g1", name="北门")
            service.set_capacity(request_id="cap", actor_id="a1", entrance_id="g1",
                                 visit_date=DATE, slot=AM, capacity=5)
            service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                            visit_date=DATE, slot=AM, order_type="group",
                            quantity=2, party_name="重启团", hold_seconds=300)
            database.close()

            later = ReservationService(
                Database(path), FixedClock(datetime(2026, 10, 7, 20, 6, tzinfo=TZ))
            )
            self.assertEqual(1, later.expire_due_reservations()["count"])
            self.assertEqual(5, later.availability("g1", DATE, AM).usable)
            later.database.close()

    # ----- 取消 -----

    def test_cancel_releases_all_items_with_reason(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="group",
                             quantity=3, party_name="取消团")
        self.service.confirm_payment(request_id="pay1", order_id="o1")
        self.service.cancel_order(request_id="c1", order_id="o1")
        self.assertEqual(5, self.service.availability("g1", DATE, AM).usable)
        for item in self.service.get_order("o1").items:
            self.assertEqual("released", item.state)
            self.assertEqual("cancelled", item.released_reason)
        with self.assertRaises(OrderStateError):
            self.service.cancel_order(request_id="c2", order_id="o1")

    # ----- 改期 -----

    def test_reschedule_moves_confirmed_order_atomically(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=2, party_name="改期人")
        with self.assertRaises(OrderStateError):
            self.service.reschedule_order(request_id="m0", order_id="o1",
                                          new_order_id="o1b", entrance_id="g1",
                                          visit_date=DATE, slot=PM)
        self.service.confirm_payment(request_id="pay1", order_id="o1")
        receipt = self.service.reschedule_order(request_id="m1", order_id="o1",
                                                new_order_id="o1b", entrance_id="g1",
                                                visit_date=DATE, slot=PM)
        self.assertEqual("o1b", receipt.resource_id)
        self.assertEqual(5, self.service.availability("g1", DATE, AM).usable)
        self.assertEqual(2, self.service.availability("g1", DATE, PM).occupied)
        self.assertEqual("rescheduled", self.service.get_order("o1").state)
        new_order = self.service.get_order("o1b")
        self.assertEqual("confirmed", new_order.state)
        am_ledger = self.service.ledger("g1", DATE, AM)
        self.assertIn("reschedule_out", {e.reason for e in am_ledger})
        pm_ledger = self.service.ledger("g1", DATE, PM)
        self.assertTrue(all(e.reason == "reschedule_in" for e in pm_ledger))
        self.assertEqual("o1", pm_ledger[0].related_order_id)

    def test_reschedule_failure_leaves_original_intact(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=2, party_name="改期人")
        self.service.confirm_payment(request_id="pay1", order_id="o1")
        # 下午容量只有 10，先占满 9 个，再尝试改入 2 个应失败。
        self.service.reserve(request_id="r2", order_id="o2", entrance_id="g1",
                             visit_date=DATE, slot=PM, order_type="group",
                             quantity=9, party_name="占满下午团")
        with self.assertRaises(CapacityExceededError):
            self.service.reschedule_order(request_id="m-bad", order_id="o1",
                                          new_order_id="o1b", entrance_id="g1",
                                          visit_date=DATE, slot=PM)
        self.assertEqual("confirmed", self.service.get_order("o1").state)
        self.assertEqual(2, self.service.availability("g1", DATE, AM).occupied)
        with self.assertRaises(NotFoundError):
            self.service.get_order("o1b")

    def test_reschedule_to_same_slot_or_same_order_id_is_rejected(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=1, party_name="改期人")
        self.service.confirm_payment(request_id="pay1", order_id="o1")
        with self.assertRaises(ValidationError):
            self.service.reschedule_order(request_id="m-same-id", order_id="o1",
                                          new_order_id="o1", entrance_id="g1",
                                          visit_date=DATE, slot=PM)
        with self.assertRaises(ValidationError):
            self.service.reschedule_order(request_id="m-same-slot", order_id="o1",
                                          new_order_id="o1c", entrance_id="g1",
                                          visit_date=DATE, slot=AM)
        # 参数校验失败不留任何痕迹，订单仍在原时段。
        self.assertEqual(1, self.service.availability("g1", DATE, AM).occupied)

    # ----- 台账追溯 -----

    def test_item_history_explains_why_occupied_and_released(self):
        receipt = self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                                       visit_date=DATE, slot=AM, order_type="group",
                                       quantity=1, party_name="追溯团")
        item_id = receipt.response["items"][0]["item_id"]
        self.service.confirm_payment(request_id="pay1", order_id="o1")
        self.service.cancel_order(request_id="c1", order_id="o1")
        history = self.service.item_history(item_id)
        self.assertEqual(["reserved", "confirmed", "cancelled"], [e.reason for e in history])
        self.assertEqual([1, 1, 0], [e.balance_after for e in history])

    # ----- 权限 -----

    def test_unknown_actor_cannot_configure(self):
        with self.assertRaises((PermissionDenied, NotFoundError)):
            self.service.register_entrance(request_id="req-x", actor_id="nobody",
                                           entrance_id="g9", name="歪门")

    # ----- 并发裁决 -----

    def test_concurrent_races_for_last_spots_never_oversell(self):
        threads = []
        results: list[str] = []
        barrier = threading.Barrier(8)

        def buyer(index: int) -> None:
            barrier.wait()
            try:
                self.service.reserve(
                    request_id=f"race-{index}", order_id=f"o-race-{index}", entrance_id="g1",
                    visit_date=DATE, slot=AM, order_type=("group" if index % 2 else "individual"),
                    quantity=1, party_name=f"抢票人{index}",
                )
                results.append("won")
            except CapacityExceededError:
                results.append("lost")

        for i in range(8):
            threads.append(threading.Thread(target=buyer, args=(i,)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(5, results.count("won"))
        self.assertEqual(3, results.count("lost"))
        slot = self.service.availability("g1", DATE, AM)
        self.assertEqual(5, slot.reserved + slot.occupied)
        ledger_balance = self.database.connection.execute(
            "SELECT COALESCE(SUM(delta),0) AS b FROM capacity_ledger "
            "WHERE entrance_id='g1' AND visit_date=? AND slot=?", (DATE, AM)
        ).fetchone()["b"]
        self.assertEqual(5, ledger_balance)

    # ----- 审计 -----

    def test_audit_chain_stays_valid(self):
        self.service.reserve(request_id="r1", order_id="o1", entrance_id="g1",
                             visit_date=DATE, slot=AM, order_type="individual",
                             quantity=1, party_name="审计人")
        self.service.confirm_payment(request_id="p1", order_id="o1")
        self.service.cancel_order(request_id="c1", order_id="o1")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreaterEqual(count, 5)


if __name__ == "__main__":
    unittest.main()
