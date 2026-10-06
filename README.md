# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/commands`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 接近事件 → 规避指令 → 运营方回执

协调员批准规避方案（`approve`，需带 `fuel_cost_m_s` 与 ISO 区间 `maneuver_window`，如 `2026-09-28T08:00:00Z/2026-09-28T09:00:00Z`）后系统直接下达规避指令（`in_flight`），窗口按卫星（默认主物体，可用 `target_object_id` 指定）互斥：

- 同一物体同一时间只能有一条在途指令。两个协调员同时提交重叠窗口时，数据库事务内只让一条成立（`command_issued`），落选指令记为 `voided`、事件退回 `returned` 协调重排，并返回 `409 window_overlap`（含胜出指令信息）。窗口按半开区间 `[start, end)` 判定，端点相接不冲突。
- 运营方通过 `receipt` 登记回执：`acked` 闭环为 `resolved` 并释放窗口；`failed` 必须带 `reason`，指令与事件进入 `failed`、退回协调，窗口继续保留不被抢占。
- 运营方用 `retry` 重试，必须沿用原 `command_ref`（不符返回 `command_ref_mismatch`），复用同一条指令、同一窗口，不会产生第二条占窗指令，回执历史里累计重试次数。
- 协调员可用 `void_command`（需 `reason`）手动作废在途/失败指令，释放窗口并退回协调。
- 分析员 `report_revision` 提交新观测后立即重算风险等级；若等级低于指令下达时的等级，在途指令自动作废（`voided`）、释放窗口、事件退回 `returned`。
- `GET /api/items/<id>` 详情包含 `in_flight_command`（在途指令）、`occupied_windows`（同卫星当前占用窗口）、`commands`（指令链及每条指令的回执状态、失败原因、重试记录）。

事件状态：`pending → assessed → in_flight → resolved/failed`；失败或落选后为 `returned`，协调员可用不重叠窗口重新 `approve`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、窗口重叠仲裁、20 路并发批准只成立一条、失败回执与同指令重试、新观测降级作废指令释放窗口。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
