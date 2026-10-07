"""景区分时预约领域服务。

容量裁决模型
------------
- 容量以「入口 x 日期 x 时段」为单位配置，个人票与团体票写入同一张
  ``ticket_items`` 表与同一个 ``capacity_ledger``，天然共享账本。
- 预留（hold）当场占用名额并计入余额；支付确认只是把名额状态从 held
  推进到 occupied，不重复扣减；过期/取消/改期则写反向流水释放。
- 每个名额的每次变动都在台账中留下一笔带原因的流水，余额逐笔演进，
  运营可据此回答"这个名额为什么被占用、又为什么被释放"。
- 所有写操作在 ``BEGIN IMMEDIATE`` 事务内完成容量检查与扣减，SQLite
  写锁全库唯一，因此并发抢最后名额时按获取写锁的先后稳定裁决，容量
  永远不可能超卖。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest, verify_chain
from .clock import Clock, SystemClock
from .errors import (CapacityExceededError, ConflictError, NotFoundError,
                     OrderStateError, PermissionDenied, ValidationError)
from .models import (LedgerEntry, Order, SlotAvailability, TicketItem,
                     TICKET_TYPES, WriteReceipt)
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLOT = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
ROLES = frozenset({"admin", "operator"})

# 预留默认保留 15 分钟，过期由可注入时钟驱动释放。
DEFAULT_HOLD_SECONDS = 15 * 60
MAX_QUANTITY = 1000


class ReservationService:
    """协调容量账本、幂等、事务、过期与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ----- 基础工具 -----

    def _now(self) -> datetime:
        return self.clock.now()

    def _stamp(self) -> str:
        # 所有落库时间统一为带偏移的 UTC ISO-8601，保证字符串比较与机器时区无关。
        return self._now().astimezone(timezone.utc).isoformat()

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: str) -> str:
        value = str(value).strip()
        if not DATE.fullmatch(value):
            raise ValidationError("visit_date 必须是 YYYY-MM-DD")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("visit_date 不是合法日期") from exc
        return value

    def _slot(self, value: str) -> str:
        value = str(value).strip()
        if not SLOT.fullmatch(value):
            raise ValidationError("slot 必须形如 09:00-12:00")
        start, end = value[:5], value[6:]
        if start >= end:
            raise ValidationError("slot 的开始时间必须早于结束时间")
        return value

    def _staff(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM staff WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("运营人员不存在")
        if not row["active"]:
            raise PermissionDenied("运营人员已停用")
        return {"actor_id": row["actor_id"], "role": row["role"]}

    def _require_staff(self, connection, actor_id: str) -> dict[str, Any]:
        count = connection.execute("SELECT COUNT(*) AS c FROM staff").fetchone()["c"]
        if count == 0:
            if actor_id != "bootstrap":
                raise PermissionDenied("首次建档必须使用 bootstrap")
            return {"actor_id": "bootstrap", "role": "admin"}
        actor = self._staff(connection, actor_id)
        if actor["role"] not in ROLES:
            raise PermissionDenied("当前角色不能执行该动作")
        return actor

    def _replay(self, connection, *, request_id: str, action: str,
                payload: dict[str, Any]) -> WriteReceipt | None:
        """若 request_id 已成功处理过，返回原始收据；必须在业务裁决之前调用。

        这样即使重试时槽位已被别人占满或订单状态已推进，重复请求拿到的
        仍是第一次的结果，绝不会再次扣减，也不会被当前状态错误拒绝。
        """

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, action, row["resource_type"], row["resource_id"],
                            True, json.loads(row["response_json"]))

    def _store_receipt(self, connection, *, request_id: str, action: str,
                       payload: dict[str, Any], resource_type: str, resource_id: str,
                       response: dict[str, Any]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._stamp()),
        )
        return WriteReceipt(request_id, action, resource_type, resource_id, False, response)

    # ----- 容量与台账 -----

    def _slot_row(self, connection, entrance_id: str, visit_date: str, slot: str):
        row = connection.execute(
            "SELECT * FROM slot_capacity WHERE entrance_id=? AND visit_date=? AND slot=?",
            (entrance_id, visit_date, slot),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"时段未配置容量：{entrance_id} {visit_date} {slot}")
        return row

    def _active_counts(self, connection, entrance_id: str, visit_date: str, slot: str) -> tuple[int, int]:
        """返回 (预留中数量, 已占用数量)，两者之和即当前在账名额。"""

        rows = connection.execute(
            "SELECT state, COUNT(*) AS c FROM ticket_items "
            "WHERE entrance_id=? AND visit_date=? AND slot=? AND state IN ('held','occupied') "
            "GROUP BY state",
            (entrance_id, visit_date, slot),
        ).fetchall()
        held = occupied = 0
        for row in rows:
            if row["state"] == "held":
                held = row["c"]
            else:
                occupied = row["c"]
        return held, occupied

    def _ledger_balance(self, connection, entrance_id: str, visit_date: str, slot: str) -> int:
        """从台账流水重放该时段的在账余额，作为扣减裁决的唯一依据。"""

        row = connection.execute(
            "SELECT COALESCE(SUM(delta),0) AS balance FROM capacity_ledger "
            "WHERE entrance_id=? AND visit_date=? AND slot=?",
            (entrance_id, visit_date, slot),
        ).fetchone()
        return int(row["balance"])

    def _post_ledger(self, connection, *, entrance_id: str, visit_date: str, slot: str,
                     item_id: str, order_id: str, request_id: str, direction: str,
                     reason: str, delta: int, related_order_id: str | None,
                     occurred_at: str) -> None:
        balance = self._ledger_balance(connection, entrance_id, visit_date, slot) + delta
        if balance < 0:
            # 防御性不变量：台账不允许出现负余额。
            raise ConflictError("容量台账余额不能为负")
        connection.execute(
            "INSERT INTO capacity_ledger(entrance_id,visit_date,slot,item_id,order_id,request_id,"
            "direction,reason,related_order_id,delta,balance_after,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (entrance_id, visit_date, slot, item_id, order_id, request_id, direction, reason,
             related_order_id, delta, balance, occurred_at),
        )

    def _release_items(self, connection, *, order_row, reason: str, request_id: str,
                       stamp: str) -> int:
        """释放订单名下所有仍在账名额，返回释放数量。"""

        released = 0
        items = connection.execute(
            "SELECT * FROM ticket_items WHERE order_id=? AND state IN ('held','occupied')",
            (order_row["order_id"],),
        ).fetchall()
        for item in items:
            connection.execute(
                "UPDATE ticket_items SET state='released', released_reason=?, released_at=?, "
                "updated_at=? WHERE item_id=?",
                (reason, stamp, stamp, item["item_id"]),
            )
            self._post_ledger(
                connection, entrance_id=item["entrance_id"], visit_date=item["visit_date"],
                slot=item["slot"], item_id=item["item_id"], order_id=order_row["order_id"],
                request_id=request_id, direction="release", reason=reason, delta=-1,
                related_order_id=None, occurred_at=stamp,
            )
            released += 1
        return released

    def _sweep_expired(self, connection, now: datetime) -> list[str]:
        """把所有 expires_at 已到的预留单释放并置为 expired。幂等。"""

        now_text = now.isoformat()
        due = connection.execute(
            "SELECT * FROM orders WHERE state='reserved' AND expires_at IS NOT NULL "
            "AND expires_at <= ? ORDER BY expires_at, order_id",
            (now_text,),
        ).fetchall()
        expired_orders: list[str] = []
        for order in due:
            stamp = self._stamp()
            count = self._release_items(connection, order_row=order, reason="expired",
                                        request_id="system:expiry", stamp=stamp)
            connection.execute(
                "UPDATE orders SET state='expired', updated_at=? WHERE order_id=?",
                (stamp, order["order_id"]),
            )
            append_event(connection, actor_id="system", action="reservation.expired",
                         resource_type="order", resource_id=order["order_id"],
                         detail={"released": count, "expires_at": order["expires_at"]},
                         occurred_at=stamp)
            expired_orders.append(order["order_id"])
        return expired_orders

    # ----- 运营配置 -----

    def register_staff(self, *, request_id: str, actor_id: str, new_actor_id: str,
                       display_name: str, role: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "new_actor_id": new_actor_id,
                   "display_name": display_name, "role": role}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="register_staff",
                                  payload=payload)
            if replay is not None:
                return replay
            actor = self._require_staff(connection, actor_id)
            new_actor_id = self._identifier(new_actor_id, "new_actor_id")
            display_name = self._text(display_name, "display_name")
            if role not in ROLES:
                raise ValidationError("role 只允许 admin 或 operator")

            stamp = self._stamp()
            try:
                connection.execute(
                    "INSERT INTO staff(actor_id,display_name,role,active,created_at) "
                    "VALUES(?,?,?,1,?)",
                    (new_actor_id, display_name, role, stamp),
                )
            except Exception as exc:
                raise ConflictError("运营人员编号已经存在") from exc
            append_event(connection, actor_id=actor["actor_id"], action="staff.registered",
                         resource_type="staff", resource_id=new_actor_id,
                         detail={"display_name": display_name, "role": role}, occurred_at=stamp)
            response = {"actor_id": new_actor_id}
            return self._store_receipt(connection, request_id=request_id, action="register_staff",
                                       payload=payload, resource_type="staff",
                                       resource_id=new_actor_id, response=response)

    def register_entrance(self, *, request_id: str, actor_id: str,
                          entrance_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "entrance_id": entrance_id, "name": name}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="register_entrance",
                                  payload=payload)
            if replay is not None:
                return replay
            actor = self._require_staff(connection, actor_id)
            entrance_id = self._identifier(entrance_id, "entrance_id")
            name = self._text(name, "name")

            stamp = self._stamp()
            try:
                connection.execute(
                    "INSERT INTO entrances(entrance_id,name,created_at) VALUES(?,?,?)",
                    (entrance_id, name, stamp),
                )
            except Exception as exc:
                raise ConflictError("入口编号已经存在") from exc
            append_event(connection, actor_id=actor["actor_id"], action="entrance.registered",
                         resource_type="entrance", resource_id=entrance_id,
                         detail={"name": name}, occurred_at=stamp)
            response = {"entrance_id": entrance_id}
            return self._store_receipt(connection, request_id=request_id, action="register_entrance",
                                       payload=payload, resource_type="entrance",
                                       resource_id=entrance_id, response=response)

    def set_capacity(self, *, request_id: str, actor_id: str, entrance_id: str,
                     visit_date: str, slot: str, capacity: int) -> WriteReceipt:
        """维护入口/日期/时段容量。容量可上调；下调不得低于在账名额。"""

        payload = {"actor_id": actor_id, "entrance_id": entrance_id, "visit_date": visit_date,
                   "slot": slot, "capacity": capacity}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="set_capacity",
                                  payload=payload)
            if replay is not None:
                return replay
            actor = self._require_staff(connection, actor_id)
            entrance_id = self._identifier(entrance_id, "entrance_id")
            visit_date = self._date(visit_date)
            slot = self._slot(slot)
            if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
                raise ValidationError("capacity 必须是非负整数")
            entrance = connection.execute(
                "SELECT 1 FROM entrances WHERE entrance_id=?", (entrance_id,)
            ).fetchone()
            if entrance is None:
                raise NotFoundError("入口不存在")
            self._sweep_expired(connection, self._now())
            existing = connection.execute(
                "SELECT * FROM slot_capacity WHERE entrance_id=? AND visit_date=? AND slot=?",
                (entrance_id, visit_date, slot),
            ).fetchone()
            in_use = self._ledger_balance(connection, entrance_id, visit_date, slot)
            if capacity < in_use:
                raise ValidationError(f"容量不能下调到在账名额以下：当前在账 {in_use}")

            stamp = self._stamp()
            if existing is None:
                connection.execute(
                    "INSERT INTO slot_capacity(entrance_id,visit_date,slot,capacity,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (entrance_id, visit_date, slot, capacity, stamp, stamp),
                )
            else:
                connection.execute(
                    "UPDATE slot_capacity SET capacity=?, updated_at=? "
                    "WHERE entrance_id=? AND visit_date=? AND slot=?",
                    (capacity, stamp, entrance_id, visit_date, slot),
                )
            append_event(connection, actor_id=actor["actor_id"], action="capacity.configured",
                         resource_type="slot_capacity",
                         resource_id=f"{entrance_id}:{visit_date}:{slot}",
                         detail={"capacity": capacity,
                                 "previous_capacity": existing["capacity"] if existing else None},
                         occurred_at=stamp)
            response = {"entrance_id": entrance_id, "visit_date": visit_date,
                        "slot": slot, "capacity": capacity}
            return self._store_receipt(connection, request_id=request_id, action="set_capacity",
                                       payload=payload, resource_type="slot_capacity",
                                       resource_id=f"{entrance_id}:{visit_date}:{slot}",
                                       response=response)

    # ----- 预约生命周期 -----

    def reserve(self, *, request_id: str, order_id: str, entrance_id: str, visit_date: str,
                slot: str, order_type: str, quantity: int, party_name: str,
                channel_id: str | None = None, hold_seconds: int | None = None) -> WriteReceipt:
        """占位：在事务内检查容量并逐名额入账，超容量整单失败。"""

        payload = {"order_id": order_id, "entrance_id": entrance_id, "visit_date": visit_date,
                   "slot": slot, "order_type": order_type, "quantity": quantity,
                   "party_name": party_name, "channel_id": channel_id}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="reserve",
                                  payload=payload)
            if replay is not None:
                return replay
            order_id = self._identifier(order_id, "order_id")
            entrance_id = self._identifier(entrance_id, "entrance_id")
            visit_date = self._date(visit_date)
            slot = self._slot(slot)
            party_name = self._text(party_name, "party_name")
            if order_type not in TICKET_TYPES:
                raise ValidationError("order_type 只允许 individual 或 group")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or not (0 < quantity <= MAX_QUANTITY):
                raise ValidationError(f"quantity 必须是 1..{MAX_QUANTITY} 的整数")
            hold = DEFAULT_HOLD_SECONDS if hold_seconds is None else hold_seconds
            if not isinstance(hold, int) or hold <= 0 or hold > 24 * 3600:
                raise ValidationError("hold_seconds 必须是 1..86400 的整数")

            self._sweep_expired(connection, self._now())
            self._slot_row(connection, entrance_id, visit_date, slot)
            in_use = self._ledger_balance(connection, entrance_id, visit_date, slot)
            capacity_row = connection.execute(
                "SELECT capacity FROM slot_capacity WHERE entrance_id=? AND visit_date=? AND slot=?",
                (entrance_id, visit_date, slot),
            ).fetchone()
            if in_use + quantity > capacity_row["capacity"]:
                raise CapacityExceededError(
                    f"剩余容量不足：在账 {in_use}/{capacity_row['capacity']}，申请 {quantity}"
                )
            if connection.execute("SELECT 1 FROM orders WHERE order_id=?", (order_id,)).fetchone():
                raise ConflictError("订单编号已经存在")

            stamp = self._stamp()
            expires_at = (self._now() + timedelta(seconds=hold)).isoformat()
            connection.execute(
                "INSERT INTO orders(order_id,entrance_id,visit_date,slot,order_type,"
                "party_name,quantity,state,request_id,channel_id,created_at,updated_at,"
                "expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, entrance_id, visit_date, slot, order_type, party_name,
                 quantity, "reserved", request_id, channel_id, stamp, stamp, expires_at),
            )
            for _ in range(quantity):
                item_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ticket_items(item_id,order_id,entrance_id,visit_date,slot,"
                    "ticket_type,state,request_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (item_id, order_id, entrance_id, visit_date, slot, order_type, "held",
                     request_id, stamp, stamp),
                )
                self._post_ledger(
                    connection, entrance_id=entrance_id, visit_date=visit_date, slot=slot,
                    item_id=item_id, order_id=order_id, request_id=request_id,
                    direction="hold", reason="reserved", delta=1, related_order_id=None,
                    occurred_at=stamp,
                )
            append_event(connection, actor_id=channel_id or "guest",
                         action="reservation.placed", resource_type="order",
                         resource_id=order_id,
                         detail={"entrance_id": entrance_id, "visit_date": visit_date,
                                 "slot": slot, "order_type": order_type, "quantity": quantity,
                                 "expires_at": expires_at},
                         occurred_at=stamp)
            response = self._order_response(connection, order_id)
            return self._store_receipt(connection, request_id=request_id, action="reserve",
                                       payload=payload, resource_type="order",
                                       resource_id=order_id, response=response)

    def confirm_payment(self, *, request_id: str, order_id: str) -> WriteReceipt:
        """支付确认：held -> occupied，写 mark 流水但余额不变，绝不重复扣减。"""

        payload = {"order_id": order_id}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="confirm_payment",
                                  payload=payload)
            if replay is not None:
                return replay
            order_id = self._identifier(order_id, "order_id")
            self._sweep_expired(connection, self._now())
            order = connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if order is None:
                raise NotFoundError("订单不存在")
            if order["state"] != "reserved":
                raise OrderStateError(f"订单状态为 {order['state']}，不能支付确认")

            stamp = self._stamp()
            connection.execute(
                "UPDATE orders SET state='confirmed', confirmed_at=?, updated_at=?, expires_at=NULL "
                "WHERE order_id=?",
                (stamp, stamp, order_id),
            )
            items = connection.execute(
                "SELECT * FROM ticket_items WHERE order_id=? AND state='held'", (order_id,)
            ).fetchall()
            for item in items:
                connection.execute(
                    "UPDATE ticket_items SET state='occupied', updated_at=? WHERE item_id=?",
                    (stamp, item["item_id"]),
                )
                self._post_ledger(
                    connection, entrance_id=item["entrance_id"], visit_date=item["visit_date"],
                    slot=item["slot"], item_id=item["item_id"], order_id=order_id,
                    request_id=request_id, direction="mark", reason="confirmed", delta=0,
                    related_order_id=None, occurred_at=stamp,
                )
            append_event(connection, actor_id="system", action="reservation.confirmed",
                         resource_type="order", resource_id=order_id,
                         detail={"quantity": len(items)}, occurred_at=stamp)
            response = self._order_response(connection, order_id)
            return self._store_receipt(connection, request_id=request_id, action="confirm_payment",
                                       payload=payload, resource_type="order",
                                       resource_id=order_id, response=response)

    def cancel_order(self, *, request_id: str, order_id: str) -> WriteReceipt:
        """整单取消：释放订单全部在账名额（预留或已确认均可）。"""

        payload = {"order_id": order_id}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="cancel_order",
                                  payload=payload)
            if replay is not None:
                return replay
            order_id = self._identifier(order_id, "order_id")
            self._sweep_expired(connection, self._now())
            order = connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if order is None:
                raise NotFoundError("订单不存在")
            if order["state"] not in ("reserved", "confirmed"):
                raise OrderStateError(f"订单状态为 {order['state']}，不能取消")

            stamp = self._stamp()
            released = self._release_items(connection, order_row=order, reason="cancelled",
                                            request_id=request_id, stamp=stamp)
            connection.execute(
                "UPDATE orders SET state='cancelled', updated_at=? WHERE order_id=?",
                (stamp, order_id),
            )
            append_event(connection, actor_id="system", action="reservation.cancelled",
                         resource_type="order", resource_id=order_id,
                         detail={"released": released}, occurred_at=stamp)
            response = self._order_response(connection, order_id)
            return self._store_receipt(connection, request_id=request_id, action="cancel_order",
                                       payload=payload, resource_type="order",
                                       resource_id=order_id, response=response)

    def reschedule_order(self, *, request_id: str, order_id: str, new_order_id: str,
                         entrance_id: str, visit_date: str, slot: str) -> WriteReceipt:
        """改期：原子地释放原时段全部名额并在目标时段重新占用。

        只有已支付（confirmed）的整单可以改期；目标时段容量不足时整单失败，
        原订单不受影响。原名额写 reschedule_out，新名额写 reschedule_in 并
        互相指向对方订单，台账可完整还原改期链路。
        """

        payload = {"order_id": order_id, "new_order_id": new_order_id,
                   "entrance_id": entrance_id, "visit_date": visit_date, "slot": slot}
        with self.database.transaction() as connection:
            replay = self._replay(connection, request_id=request_id, action="reschedule_order",
                                  payload=payload)
            if replay is not None:
                return replay
            order_id = self._identifier(order_id, "order_id")
            new_order_id = self._identifier(new_order_id, "new_order_id")
            if new_order_id == order_id:
                raise ValidationError("new_order_id 不能与原订单号相同")
            entrance_id = self._identifier(entrance_id, "entrance_id")
            visit_date = self._date(visit_date)
            slot = self._slot(slot)
            self._sweep_expired(connection, self._now())
            order = connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if order is None:
                raise NotFoundError("原订单不存在")
            if (entrance_id, visit_date, slot) == (order["entrance_id"], order["visit_date"], order["slot"]):
                raise ValidationError("改期目标与原时段完全相同，无需改期")
            if order["state"] != "confirmed":
                raise OrderStateError(f"订单状态为 {order['state']}，只有已支付订单可以改期")
            target = self._slot_row(connection, entrance_id, visit_date, slot)
            in_use = self._ledger_balance(connection, entrance_id, visit_date, slot)
            quantity = order["quantity"]
            if in_use + quantity > target["capacity"]:
                raise CapacityExceededError(
                    f"目标时段剩余容量不足：在账 {in_use}/{target['capacity']}，申请 {quantity}"
                )
            if connection.execute("SELECT 1 FROM orders WHERE order_id=?", (new_order_id,)).fetchone():
                raise ConflictError("新订单编号已经存在")

            stamp = self._stamp()
            # 1) 释放原时段名额。
            moved = self._release_items(connection, order_row=order, reason="reschedule_out",
                                        request_id=request_id, stamp=stamp)
            # 2) 在目标时段用新订单号重建已确认订单与名额。
            connection.execute(
                "INSERT INTO orders(order_id,entrance_id,visit_date,slot,order_type,party_name,"
                "quantity,state,request_id,channel_id,created_at,updated_at,confirmed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_order_id, entrance_id, visit_date, slot, order["order_type"],
                 order["party_name"], quantity, "confirmed", request_id, order["channel_id"],
                 stamp, stamp, stamp),
            )
            for _ in range(moved):
                item_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ticket_items(item_id,order_id,entrance_id,visit_date,slot,"
                    "ticket_type,state,request_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (item_id, new_order_id, entrance_id, visit_date, slot,
                     order["order_type"], "occupied", request_id, stamp, stamp),
                )
                self._post_ledger(
                    connection, entrance_id=entrance_id, visit_date=visit_date, slot=slot,
                    item_id=item_id, order_id=new_order_id, request_id=request_id,
                    direction="hold", reason="reschedule_in", delta=1,
                    related_order_id=order_id, occurred_at=stamp,
                )
            connection.execute(
                "UPDATE orders SET state='rescheduled', superseded_by=?, updated_at=? "
                "WHERE order_id=?",
                (new_order_id, stamp, order_id),
            )
            append_event(connection, actor_id="system", action="reservation.rescheduled",
                         resource_type="order", resource_id=order_id,
                         detail={"new_order_id": new_order_id, "entrance_id": entrance_id,
                                 "visit_date": visit_date, "slot": slot, "quantity": moved},
                         occurred_at=stamp)
            response = self._order_response(connection, new_order_id)
            return self._store_receipt(connection, request_id=request_id, action="reschedule_order",
                                       payload=payload, resource_type="order",
                                       resource_id=new_order_id, response=response)

    def expire_due_reservations(self) -> dict[str, Any]:
        """按可注入当前时间释放所有到期预留（也可由定时任务或重启后调用）。"""

        with self.database.transaction() as connection:
            expired = self._sweep_expired(connection, self._now())
        return {"expired_orders": expired, "count": len(expired), "now": self._stamp()}

    # ----- 查询 -----

    def _order_response(self, connection, order_id: str) -> dict[str, Any]:
        order = self.get_order(order_id, connection=connection)
        return {
            "order_id": order.order_id, "entrance_id": order.entrance_id,
            "visit_date": order.visit_date, "slot": order.slot,
            "order_type": order.order_type, "party_name": order.party_name,
            "quantity": order.quantity, "state": order.state,
            "expires_at": order.expires_at, "confirmed_at": order.confirmed_at,
            "superseded_by": order.superseded_by,
            "items": [item.__dict__ for item in order.items],
        }

    def get_order(self, order_id: str, *, connection=None) -> Order:
        if connection is None:
            # 与写事务共用同一把可重入锁，避免读到提交前的中间态。
            with self.database.lock():
                return self._load_order(self.database.connection, order_id)
        return self._load_order(connection, order_id)

    def _load_order(self, connection, order_id: str) -> Order:
        row = connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("订单不存在")
        items = [self._item(row) for row in connection.execute(
            "SELECT * FROM ticket_items WHERE order_id=? ORDER BY item_id", (order_id,))]
        return Order(row["order_id"], row["entrance_id"], row["visit_date"], row["slot"],
                     row["order_type"], row["party_name"], row["quantity"], row["state"],
                     row["request_id"], row["created_at"], row["updated_at"], row["expires_at"],
                     row["confirmed_at"], row["superseded_by"], items)

    @staticmethod
    def _item(row) -> TicketItem:
        return TicketItem(row["item_id"], row["order_id"], row["entrance_id"], row["visit_date"],
                          row["slot"], row["ticket_type"], row["state"], row["request_id"],
                          row["created_at"], row["updated_at"], row["released_reason"],
                          row["released_at"])

    def availability(self, entrance_id: str, visit_date: str, slot: str) -> SlotAvailability:
        """返回时段容量对账：capacity = occupied + reserved + usable。"""

        entrance_id = self._identifier(entrance_id, "entrance_id")
        visit_date = self._date(visit_date)
        slot = self._slot(slot)
        with self.database.transaction() as connection:
            self._sweep_expired(connection, self._now())
            row = self._slot_row(connection, entrance_id, visit_date, slot)
            held, occupied = self._active_counts(connection, entrance_id, visit_date, slot)
            ledger_balance = self._ledger_balance(connection, entrance_id, visit_date, slot)
        # 台账余额与名额状态必须时刻互相印证。
        if ledger_balance != held + occupied:
            raise ConflictError("容量台账与名额状态不一致")
        return SlotAvailability(entrance_id, visit_date, slot, row["capacity"], occupied, held,
                                row["capacity"] - occupied - held)

    def ledger(self, entrance_id: str, visit_date: str, slot: str,
               after_sequence: int = 0) -> list[LedgerEntry]:
        """按时段追溯全部名额流水：为何占用、为何释放、余额如何演进。"""

        entrance_id = self._identifier(entrance_id, "entrance_id")
        visit_date = self._date(visit_date)
        slot = self._slot(slot)
        with self.database.lock():
            rows = self.database.connection.execute(
                "SELECT * FROM capacity_ledger WHERE entrance_id=? AND visit_date=? AND slot=? "
                "AND sequence>? ORDER BY sequence",
                (entrance_id, visit_date, slot, after_sequence),
            ).fetchall()
        return [LedgerEntry(r["sequence"], r["entrance_id"], r["visit_date"], r["slot"],
                            r["item_id"], r["order_id"], r["request_id"], r["direction"],
                            r["reason"], r["related_order_id"], r["delta"], r["balance_after"],
                            r["occurred_at"]) for r in rows]

    def item_history(self, item_id: str) -> list[LedgerEntry]:
        """追溯单个名额的完整变动链。"""

        item_id = self._identifier(item_id, "item_id")
        with self.database.lock():
            rows = self.database.connection.execute(
                "SELECT * FROM capacity_ledger WHERE item_id=? ORDER BY sequence", (item_id,)
            ).fetchall()
        if not rows:
            raise NotFoundError("名额不存在或没有台账记录")
        return [LedgerEntry(r["sequence"], r["entrance_id"], r["visit_date"], r["slot"],
                            r["item_id"], r["order_id"], r["request_id"], r["direction"],
                            r["reason"], r["related_order_id"], r["delta"], r["balance_after"],
                            r["occurred_at"]) for r in rows]

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        with self.database.lock():
            rows = self.database.connection.execute(
                "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)
            ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"],
                 "actor_id": row["actor_id"], "action": row["action"],
                 "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                 "detail": json.loads(row["detail_json"]), "previous_hash": row["previous_hash"],
                 "event_hash": row["event_hash"], "occurred_at": row["occurred_at"]}
                for row in rows]

    def verify_audit(self) -> tuple[bool, int]:
        with self.database.lock():
            return verify_chain(self.database.connection)
