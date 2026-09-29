# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 跨区域移交

适用于"案例录错区域，需要补跨区域移交"的场景，四个部分在 `transfer/` 包内独立维护：

| 模块 | 职责 |
| --- | --- |
| `transfer/records.py` | 数据记录：`transfer_requests` 表（增量升级，旧案例历史继续可查） |
| `transfer/rules.py` | 移交规则：发起/接受/拒绝/撤销的纯规则与版本判定 |
| `transfer/permissions.py` | 权限校验：角色与区域的访问规则 |
| `transfer/actions.py` | 页面操作：把每个动作编排成一个数据库事务 |

接口：

- `POST /api/cases/{id}/transfers`：原区域负责人发起，字段 `target_region`、`reason`、`read_revision`（必须等于案例当前版本）。
- `POST /api/transfers/{id}/accept`：目标区域负责人接受，接受后才切换归属；案例版本自申请后发生变化（随访、医学审核等）时接受被退回（`transfer_version_stale`），不会用旧申请覆盖最新审核。
- `POST /api/transfers/{id}/reject`：目标区域负责人拒绝，`reason` 必填。
- `POST /api/transfers/{id}/cancel`：全局管理员撤销未处理申请。
- `GET /api/transfers`、`GET /api/transfers/{id}`：按区域/角色过滤的申请查询。

行为约束：

- 同一案例同时只允许一份 `pending` 申请（部分唯一索引兜底）。
- 等待处理期间暂停随访与监管报告提交（医学审核不冻结）。
- 接受动作的"申请置已接受 + 归属切换 + 审计"在同一事务内提交，中途任何错误整体回滚，案例仍归原区域，不会留下半移交状态。
- 全部动作写入 `audit_log`（`transfer_requested/accepted/rejected/cancelled`），案例详情附带完整移交历史。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
