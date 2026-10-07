"""运行分时预约服务的离线端到端验收。

覆盖节假日典型链路：
1. 运营建档：两个入口按时段配置容量；
2. 线上个人票与旅行团团体票同时占位，共享同一容量账本；
3. 超容量占位被拒；
4. 个人票支付确认（不重复扣减）；
5. 模拟服务重启并把可注入时钟拨到预留过期之后，未完成预留被一致释放；
6. 释放出的名额可再次售出；
7. 已支付订单改期到另一时段；整单取消；
8. 台账逐笔解释每个名额为何占用/释放，审计哈希链完整。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import CapacityExceededError
from .service import ReservationService
from .storage import Database

DATE = "2026-10-08"
SLOT_MORNING = "09:00-12:00"
SLOT_AFTERNOON = "13:00-16:00"
TZ = timezone(timedelta(hours=8))


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "reservation.sqlite3"
        clock = FixedClock(datetime(2026, 10, 7, 20, 0, tzinfo=TZ))
        database = Database(db_path)
        service = ReservationService(database, clock)

        # ---- 运营建档 ----
        service.register_staff(request_id="req-admin", actor_id="bootstrap",
                               new_actor_id="admin-001", display_name="运营管理员", role="admin")
        service.register_staff(request_id="req-op", actor_id="admin-001",
                               new_actor_id="op-001", display_name="古城运营员", role="operator")
        service.register_entrance(request_id="req-gate-north", actor_id="op-001",
                                  entrance_id="north-gate", name="北门")
        service.register_entrance(request_id="req-gate-east", actor_id="op-001",
                                  entrance_id="east-gate", name="东门")
        service.set_capacity(request_id="req-cap-am", actor_id="op-001",
                             entrance_id="north-gate", visit_date=DATE, slot=SLOT_MORNING, capacity=5)
        service.set_capacity(request_id="req-cap-pm", actor_id="op-001",
                             entrance_id="north-gate", visit_date=DATE, slot=SLOT_AFTERNOON, capacity=10)

        # ---- 个人票（线上预约）与团体票（旅行团）共享同一容量 ----
        individual = service.reserve(
            request_id="req-individual", order_id="order-ind-1", entrance_id="north-gate",
            visit_date=DATE, slot=SLOT_MORNING, order_type="individual", quantity=2,
            party_name="张先生", channel_id="miniapp",
        )
        group = service.reserve(
            request_id="req-group", order_id="order-grp-1", entrance_id="north-gate",
            visit_date=DATE, slot=SLOT_MORNING, order_type="group", quantity=3,
            party_name="假日旅行团", channel_id="travel-agency-A",
        )
        full = service.availability("north-gate", DATE, SLOT_MORNING)
        over_capacity = False
        try:
            service.reserve(
                request_id="req-overflow", order_id="order-ind-2", entrance_id="north-gate",
                visit_date=DATE, slot=SLOT_MORNING, order_type="individual", quantity=1,
                party_name="李先生",
            )
        except CapacityExceededError:
            over_capacity = True

        # 个人票支付确认：余额不变，不重复扣减。
        service.confirm_payment(request_id="req-pay", order_id="order-ind-1")
        after_pay = service.availability("north-gate", DATE, SLOT_MORNING)

        # ---- 模拟服务重启：关库、重开、把时钟拨到预留过期之后 ----
        database.close()
        later_clock = FixedClock(datetime(2026, 10, 7, 21, 0, tzinfo=TZ))
        restarted = Database(db_path)
        service = ReservationService(restarted, later_clock)
        expiry = service.expire_due_reservations()
        after_expiry = service.availability("north-gate", DATE, SLOT_MORNING)
        expired_order = service.get_order("order-grp-1")

        # 团体票过期释放后，空出的名额可以再次售出。
        service.reserve(
            request_id="req-walkup", order_id="order-ind-3", entrance_id="north-gate",
            visit_date=DATE, slot=SLOT_MORNING, order_type="individual", quantity=3,
            party_name="现场散客", channel_id="onsite",
        )
        service.confirm_payment(request_id="req-pay-3", order_id="order-ind-3")

        # 已支付订单改期到下午时段。
        rescheduled = service.reschedule_order(
            request_id="req-move", order_id="order-ind-1", new_order_id="order-ind-1-pm",
            entrance_id="north-gate", visit_date=DATE, slot=SLOT_AFTERNOON,
        )
        morning_after_move = service.availability("north-gate", DATE, SLOT_MORNING)
        original_order = service.get_order("order-ind-1")

        # 整单取消现场散客订单。
        service.cancel_order(request_id="req-cancel-3", order_id="order-ind-3")
        morning_final = service.availability("north-gate", DATE, SLOT_MORNING)

        # ---- 台账追溯 + 审计校验 ----
        ledger = [entry.__dict__ for entry in service.ledger("north-gate", DATE, SLOT_MORNING)]
        balances = [entry["balance_after"] for entry in ledger]
        reasons = sorted({entry["reason"] for entry in ledger})
        sample_item = ledger[0]["item_id"]
        item_history = [entry.reason for entry in service.item_history(sample_item)]
        audit_valid, audit_count = service.verify_audit()
        restarted.close()

    return {
        "status": "ok",
        "shared_ledger_full": full.usable == 0 and full.occupied == 0 and full.reserved == 5,
        "overflow_rejected": over_capacity,
        "individual_confirmed_balance_unchanged": after_pay.occupied == 2 and after_pay.reserved == 3,
        "expired_after_restart": expiry["count"] == 1 and expired_order.state == "expired",
        "freed_quota_resold": after_expiry.usable == 3,
        "rescheduled_to_afternoon": rescheduled.resource_id == "order-ind-1-pm",
        "original_marked_rescheduled": original_order.state == "rescheduled",
        "morning_balance_after_move": morning_after_move.occupied == 3,
        "morning_final_after_cancel": morning_final.occupied == 0 and morning_final.usable == 5,
        "ledger_entries": len(ledger),
        "ledger_reasons": reasons,
        "ledger_balances_monotonic_safe": min(balances) >= 0 and balances[-1] == 0,
        "sample_item_history": item_history,
        "audit_valid": audit_valid,
        "audit_events": audit_count,
    }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected_true = [
        "shared_ledger_full", "overflow_rejected", "individual_confirmed_balance_unchanged",
        "expired_after_restart", "freed_quota_resold", "rescheduled_to_afternoon",
        "original_marked_rescheduled", "morning_final_after_cancel",
        "ledger_balances_monotonic_safe", "audit_valid",
    ]
    return 0 if result["status"] == "ok" and all(result[key] for key in expected_true) else 1


if __name__ == "__main__":
    raise SystemExit(main())
