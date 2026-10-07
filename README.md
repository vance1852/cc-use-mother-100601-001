# 景区分时预约与容量结算服务

面向节假日客流高峰的景区分时预约后端：按 **入口 × 日期 × 时间段** 维护容量与票种，
支持占位、支付确认、过期释放、改期与整单取消，**个人票与团体票共享同一容量账本**；
多入口并发抢最后名额时稳定裁决、绝不超卖，重复请求绝不重复扣减，每个名额的占用与
释放原因都可经接口逐笔追溯；服务重启后未完成预留按可注入的当前时间得到一致结果。

## 设计要点

- **共享容量账本**：容量以 `slot_capacity(entrance_id, visit_date, slot)` 为单位；
  个人票、团体票写入同一张 `ticket_items` 与 `capacity_ledger`，不分账。
- **占位即扣减**：预留（reserved）当场计入在账余额；支付确认只把名额 `held→occupied`
  并写一笔 `delta=0` 的 mark 流水，不重复扣减；过期/取消/改期写反向流水释放。
- **稳定并发裁决**：所有写操作在 `BEGIN IMMEDIATE` 事务内完成"读余额→判容量→入账"，
  配合进程内串行锁；SQLite 写锁全库唯一，先到先得，后来者排队，容量不可能超卖。
- **双重幂等**：`request_receipts` 按 `request_id` 记录首次响应，且台账上
  `(item_id, direction, reason)` 有唯一索引；重放检查先于容量/状态裁决，重试拿到
  的永远是首次结果。
- **时钟驱动过期**：时间来自可注入的 `Clock`（生产用系统时钟，测试/回放用
  `FixedClock`）；到期判断只依赖落库的 UTC 时间戳，重启后换新时钟结果一致。
- **名额级可追溯**：`capacity_ledger` 每笔流水带原因（reserved/confirmed/expired/
  cancelled/reschedule_out/reschedule_in）、槽位内演进余额和关联订单；另有哈希链
  审计日志可离线验真。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/staff` | 运营人员建档（bootstrap 引导首个 admin） |
| POST | `/entrances` | 登记入口 |
| POST | `/capacities` | 维护入口/日期/时段容量（不得下调到在账名额以下） |
| POST | `/reservations` | 占位（个人/团体，默认保留 15 分钟） |
| POST | `/reservations/confirm` | 支付确认，held→occupied，不重复扣减 |
| POST | `/reservations/cancel` | 整单取消并释放全部名额 |
| POST | `/reservations/reschedule` | 已支付订单整单改期（原子搬移） |
| POST | `/maintenance/expire-due` | 按当前时间释放所有到期预留 |
| GET | `/availability` | 时段对账：capacity = occupied + reserved + usable |
| GET | `/ledger` | 按时段追溯全部名额流水 |
| GET | `/items/{item_id}/history` | 追溯单个名额为何占用/释放 |
| GET | `/orders/{order_id}` | 订单与名下名额状态 |
| GET | `/audit-events` | 哈希链审计事件 |

所有写请求需带幂等键，例如：

```json
{ "request_id": "req-001", "order_id": "o-001", "entrance_id": "north-gate",
  "visit_date": "2026-10-08", "slot": "09:00-12:00",
  "order_type": "group", "quantity": 20, "party_name": "假日旅行团" }
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

覆盖共享账本、超容量拒绝、支付不重复扣减、**关库重开+更换注入时钟后的过期释放**、
释放名额再售、改期、整单取消与台账/审计校验：

```bash
PYTHONPATH=src python3 -m scenic_reservation.acceptance
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m scenic_reservation.api --database reservation.sqlite3 --host 127.0.0.1 --port 8080
```
