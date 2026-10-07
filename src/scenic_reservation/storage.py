"""封装 SQLite 连接、建表和事务边界。

并发策略：SQLite 同一时刻只允许一个写事务。所有写操作都使用
``BEGIN IMMEDIATE`` 立即取得写锁，先到先得，后来者在 busy_timeout 内
排队——因此多个入口同时争抢最后几个名额时，谁先提交谁赢，裁决结果
只取决于到达顺序，稳定可复现。进程内另用一把可重入锁保证同一个
连接不会被多个线程交叉使用。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS staff (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entrances (
    entrance_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 容量按 入口 x 日期 x 时段 维护；个人票与团体票共享同一行容量。
CREATE TABLE IF NOT EXISTS slot_capacity (
    entrance_id TEXT NOT NULL REFERENCES entrances(entrance_id),
    visit_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entrance_id, visit_date, slot)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    entrance_id TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    order_type TEXT NOT NULL CHECK(order_type IN ('individual', 'group')),
    party_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    state TEXT NOT NULL CHECK(state IN ('reserved', 'confirmed', 'expired', 'cancelled', 'rescheduled')),
    request_id TEXT NOT NULL,
    channel_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    confirmed_at TEXT,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_slot ON orders(entrance_id, visit_date, slot, state);
CREATE INDEX IF NOT EXISTS idx_orders_expiry ON orders(state, expires_at);
-- 每个名额一行：held=预留中，occupied=已支付占用，released=已释放。
CREATE TABLE IF NOT EXISTS ticket_items (
    item_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    entrance_id TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    ticket_type TEXT NOT NULL CHECK(ticket_type IN ('individual', 'group')),
    state TEXT NOT NULL CHECK(state IN ('held', 'occupied', 'released')),
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_reason TEXT,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_order ON ticket_items(order_id);
CREATE INDEX IF NOT EXISTS idx_items_slot ON ticket_items(entrance_id, visit_date, slot);
-- 容量台账：每个名额的占用/释放都是一笔流水。
-- hold=+1（预留即计入占用），release=-1，mark=0（支付确认等状态留痕，余额不变）。
-- balance_after 是该时段在本笔流水之后的在账总数，按 sequence 单调演进。
CREATE TABLE IF NOT EXISTS capacity_ledger (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    entrance_id TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    item_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('hold', 'release', 'mark')),
    reason TEXT NOT NULL CHECK(reason IN ('reserved', 'confirmed', 'expired',
                                          'cancelled', 'reschedule_out', 'reschedule_in')),
    related_order_id TEXT,
    delta INTEGER NOT NULL CHECK(delta IN (-1, 0, 1)),
    balance_after INTEGER NOT NULL CHECK(balance_after >= 0),
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_slot ON capacity_ledger(entrance_id, visit_date, slot, sequence);
CREATE INDEX IF NOT EXISTS idx_ledger_item ON capacity_ledger(item_id, sequence);
-- 同一笔请求对同一名额的同一动作只能入账一次：配合请求收据双保险，
-- 任何重试路径都不可能让一个名额被扣两次或放两次。
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_once
    ON capacity_ledger(item_id, direction, reason);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            # WAL 让外部读流量（运营对账）不必阻塞写事务。
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(SCHEMA)
        self._gate = threading.RLock()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """进程内串行闸门，保证同一连接上的事务不会跨线程交错。"""

        with self._gate:
            yield

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。默认立即取写锁以获得稳定裁决。"""

        with self._gate:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        with self._gate:
            self.connection.close()
