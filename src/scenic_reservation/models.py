"""定义分时预约服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 票种：个人票与团体票共享同一容量账本，区别仅体现在订单类型与台账记录上。
TICKET_TYPES = frozenset({"individual", "group"})

# 订单生命周期：预留 -> 已确认 / 已过期；已确认 -> 已取消 / 已改期。
ORDER_STATES = frozenset({"reserved", "confirmed", "expired", "cancelled", "rescheduled"})


@dataclass(frozen=True)
class Entrance:
    """景区入口。"""

    entrance_id: str
    name: str


@dataclass(frozen=True)
class CapacityConfig:
    """入口在某一日期、某一时间段的容量配置（按票种共享同一总量）。"""

    entrance_id: str
    visit_date: str
    slot: str
    capacity: int


@dataclass(frozen=True)
class SlotAvailability:
    """某时段的容量对账结果，四个数字恒满足 capacity=c+r+u。"""

    entrance_id: str
    visit_date: str
    slot: str
    capacity: int
    occupied: int
    reserved: int
    usable: int


@dataclass(frozen=True)
class TicketItem:
    """一张名额（个人票一张一人；团体票一张对应团内一个名额）。"""

    item_id: str
    order_id: str
    entrance_id: str
    visit_date: str
    slot: str
    ticket_type: str
    state: str
    request_id: str
    created_at: str
    updated_at: str
    released_reason: str | None = None
    released_at: str | None = None


@dataclass(frozen=True)
class Order:
    """一笔预约单（个人或团体），由若干名额组成。"""

    order_id: str
    entrance_id: str
    visit_date: str
    slot: str
    order_type: str
    party_name: str
    quantity: int
    state: str
    request_id: str
    created_at: str
    updated_at: str
    expires_at: str | None = None
    confirmed_at: str | None = None
    superseded_by: str | None = None
    items: list[TicketItem] = field(default_factory=list)


@dataclass(frozen=True)
class LedgerEntry:
    """容量台账中的一笔名额流水，解释某个名额为何被占用或释放。"""

    sequence: int
    entrance_id: str
    visit_date: str
    slot: str
    item_id: str
    order_id: str
    request_id: str
    direction: str          # hold | release
    reason: str             # reserved | confirmed | expired | cancelled | reschedule_out | reschedule_in
    related_order_id: str | None
    delta: int
    balance_after: int      # 该时段在本笔流水后的"已占用+预留中"总量
    occurred_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    action: str
    resource_type: str
    resource_id: str
    replayed: bool
    response: dict[str, Any] = field(default_factory=dict)
