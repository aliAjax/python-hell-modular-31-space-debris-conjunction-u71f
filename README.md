# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突、规避指令链和状态机。
- `src/repository.py`：SQLite、事务、乐观版本、窗口占用和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 规避指令链

接近事件、规避指令与运营方回执通过 `commands` 表接成一条链：

- `approve`（协调员）：批准规避方案，记录燃料预算和规避窗口（`开始/结束` ISO 时间），事件进入 `coordinating`。
- `issue_command`（协调员）：按批准窗口下发指令，指令状态为 `in_transit`，事件进入 `executing`。同一物体（主卫星）同一时间只能有一条在途指令；窗口重叠时返回 `409 window_conflict`，落选指令退回协调重排。`BEGIN IMMEDIATE` 事务保证两个协调员同时提交重叠窗口时只有一条成立。
- `receipt`（运营方）：登记回执。`result=executed` 后指令保持 `executed`，协调员可 `resolve`；`result=failed` 时必须填写 `reason`，指令标记 `failed` 并退回 `coordinating`。
- `retry_command`（协调员）：重试沿用同一条指令（相同 `command_ref`），不重复占用窗口；重试时重新检查窗口冲突，冲突则再次退回协调。
- `reschedule`（协调员）：无在途指令时调整规避窗口。
- `report_revision`（分析员）：新观测改动距离或协方差后重算等级；等级低于批准时的等级时，在途指令作废（`voided`）并释放窗口，事件退回 `coordinating`。

事件详情（`GET /api/items/<id>`）返回 `commands` 列表，包含指令状态、占用窗口和回执状态；`GET /api/state` 返回全部在途指令的占用窗口 `occupied_windows`。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

动作（`POST /api/items/<id>/actions`，payload 含 `action` 与 `expected_version`）：

| action | 角色 | 说明 |
| --- | --- | --- |
| `assess` | analyst | 风险评估 |
| `record_opinion` | operator | 运营方意见 |
| `approve` | coordinator | 批准规避方案与窗口 |
| `issue_command` | coordinator | 下发规避指令，占用窗口 |
| `receipt` | operator | 运营方回执（`executed`/`failed`，失败需 `reason`） |
| `retry_command` | coordinator | 重试失败指令，沿用同一条指令 |
| `reschedule` | coordinator | 调整规避窗口 |
| `resolve` | coordinator | 事件结案（需已执行回执） |
| `cancel` | coordinator | 取消 |
| `report_revision` | analyst | 新观测重算等级，作废不再占优的指令 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、指令下发、窗口冲突、回执失败与重试、新观测作废指令、重复告警、权限、版本冲突、过期轨道和运营方意见冲突。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
