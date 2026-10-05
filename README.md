# 景区分时预约与容量结算

这是面向节假日消费场景的后端业务基础服务，提供可追溯的状态管理、权限控制和 SQLite 持久化边界。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database holiday_service.sqlite3 --host 127.0.0.1 --port 8080
```
