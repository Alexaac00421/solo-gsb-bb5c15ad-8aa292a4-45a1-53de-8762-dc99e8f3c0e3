# 电子档案销毁裁决服务

基于 FastAPI + PostgreSQL 的档案销毁裁决服务：档案登记保留截止时间，案件可对档案发起/解除封存；批量销毁申请由服务端按裁决时刻逐件判定，仅「已到期且无生效封存」可销毁，整批结果与状态一次事务提交并保存不可变凭据。

## 一键启动

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000 （交互文档 http://localhost:8000/docs）
- PostgreSQL：localhost:5432（用户/密码/库名均为 `archives`）

表结构由应用启动时自动创建（`app` 依赖 `db` 健康检查，启动失败会自动重试等待）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://archives:archives@db:5432/archives`（compose 内） | SQLAlchemy 连接串 |

端口映射见 `docker-compose.yml`：应用 `8000:8000`，数据库 `5432:5432`。

## 裁决规则

- 逐件判定（判定基准为服务端裁决时刻，整批统一）：
  - `retain_until > 裁决时刻` → 原因 `NOT_EXPIRED`
  - 存在生效封存 → 原因 `ACTIVE_HOLD`（携带全部生效案件号）
  - 档案已销毁 → 原因 `ALREADY_DESTROYED`
  - 多种原因可同时出现；无任何原因才执行销毁
- **整批原子性**：批次内所有档案锁定（按编号排序防死锁）→ 逐件裁决 → 写入凭据与明细 → 更新档案状态，同一事务一次提交；任一档案不存在则整批 404 报错回滚，不留部分销毁
- **并发语义**：封存/解除/销毁均对档案行 `SELECT ... FOR UPDATE`，按事务提交顺序定结果——封存先提交则销毁被该封存阻止；销毁先提交则后续封存/解除报 `409 ARCHIVE_DESTROYED`
- **幂等**：`request_no` 为幂等键。同请求号同内容（档案编号集合，与顺序无关）重试返回原结果（HTTP 200）；内容不同报 `409 REQUEST_CONFLICT`
- **不可变性**：已销毁档案禁止任何变更；批次凭据与逐件明细写入后不可改，可随时复查

## API

### 登记档案 `POST /archives`

```bash
curl -X POST http://localhost:8000/archives \
  -H 'Content-Type: application/json' \
  -d '{"reg_no":"A-001","retain_until":"2020-01-01T00:00:00Z"}'
```

`retain_until` 为 ISO 8601 时间，无时区时按 UTC 处理。重复登记返回 `409 ARCHIVE_EXISTS`。

### 查询档案 `GET /archives/{reg_no}`

```bash
curl http://localhost:8000/archives/A-001
```

返回档案状态及生效封存列表。

### 发起封存 `POST /archives/{reg_no}/holds`

```bash
curl -X POST http://localhost:8000/archives/A-001/holds \
  -H 'Content-Type: application/json' \
  -d '{"case_no":"CASE-2026-001"}'
```

同一案件对同一档案重复封存幂等返回原记录（HTTP 200）；档案已销毁返回 `409 ARCHIVE_DESTROYED`。

### 解除封存 `DELETE /archives/{reg_no}/holds/{case_no}`

```bash
curl -X DELETE http://localhost:8000/archives/A-001/holds/CASE-2026-001
```

仅解除指定案件，不影响同档案其他案件的封存。无生效封存返回 `404 HOLD_NOT_FOUND`。

### 批量销毁 `POST /destruction-batches`

```bash
curl -X POST http://localhost:8000/destruction-batches \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-2026-0001","reg_nos":["A-001","A-002","A-003"]}'
```

响应示例：

```json
{
  "request_no": "REQ-2026-0001",
  "status": "COMPLETED",
  "decided_at": "2026-10-07T08:00:00.000000+00:00",
  "items": [
    {"reg_no": "A-001", "destroyed": true, "reasons": []},
    {"reg_no": "A-002", "destroyed": false, "reasons": [
      {"code": "NOT_EXPIRED", "retain_until": "2099-01-01T00:00:00+00:00"},
      {"code": "ACTIVE_HOLD", "case_nos": ["CASE-2026-001"]}
    ]},
    {"reg_no": "A-003", "destroyed": false, "reasons": [
      {"code": "ALREADY_DESTROYED", "destroyed_at": "2026-09-01T00:00:00+00:00"}
    ]}
  ]
}
```

- 批次内档案不存在：整批 `404 ARCHIVE_NOT_FOUND`（响应携带 `missing` 列表），无任何销毁发生
- 同 `request_no` 同内容重试：HTTP 200 返回原结果；不同内容：`409 REQUEST_CONFLICT`

### 复查销毁凭据 `GET /destruction-batches/{request_no}`

```bash
curl http://localhost:8000/destruction-batches/REQ-2026-0001
```

返回与提交时一致的不可变结果。

### 档案销毁历史 `GET /archives/{reg_no}/destruction-items`

```bash
curl http://localhost:8000/archives/A-001/destruction-items
```

返回该档案参与过的全部批次逐件明细（含未销毁及原因）。

## 错误码

| HTTP | code | 含义 |
|---|---|---|
| 404 | `ARCHIVE_NOT_FOUND` | 档案不存在（批次场景整批拒绝） |
| 404 | `HOLD_NOT_FOUND` | 无对应生效封存 |
| 404 | `REQUEST_NOT_FOUND` | 销毁批次不存在 |
| 409 | `ARCHIVE_EXISTS` | 档案重复登记 |
| 409 | `ARCHIVE_DESTROYED` | 档案已销毁，禁止变更 |
| 409 | `REQUEST_CONFLICT` | 请求号重用但内容不一致 |

错误响应格式：`{"error":{"code":"...","message":"...","details":{...}}}`

## 冒烟验证

服务运行后执行：

```bash
bash scripts/smoke.sh
```

覆盖建档、封存/解除、多原因并存裁决、幂等重试与冲突、整批失败回滚、封存/销毁先后提交两种并发时序、已销毁档案不可变更与历史可查等场景。

## 目录结构

```
app/
  main.py      # FastAPI 入口与路由
  services.py  # 裁决、封存、幂等、事务逻辑
  models.py    # SQLAlchemy 模型（含生效封存部分唯一索引）
  schemas.py   # 请求校验
  db.py        # 引擎与会话
scripts/smoke.sh
Dockerfile
docker-compose.yml
```
