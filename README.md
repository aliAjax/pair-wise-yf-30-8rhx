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

### 跨区域移交

- `POST /api/cases/{id}/transfers`：原区域负责人发起移交，填写 `target_region`、`reason`、`read_version`。
- `POST /api/transfers/{id}/accept`：目标区域负责人接受，接受后案例归属才切换。
- `POST /api/transfers/{id}/reject`：目标区域负责人拒绝，必须提供 `reason`。
- `POST /api/transfers/{id}/cancel`：全局管理员撤销未处理申请。
- `GET /api/transfers`、`GET /api/transfers/{id}`：移交记录查询（支持 `status`、`case_id` 过滤）。

等待目标区域受理期间，随访和新报告提交被暂停（已提交报告的幂等回放不受影响）。
申请记录发起时的读取版本，期间案例版本发生变化（随访或医学审核），接受会被退回为
`stale` 并要求按最新版本重新发起，旧申请不会覆盖最新审核。接受、拒绝、撤销均为单事务，
任一步失败整体回滚，案例仍归原区域，不存在半移交状态。移交功能按职责拆分为四个独立维护
的模块：`transfer_rules.py`（规则）、`transfer_records.py`（数据记录）、
`transfer_permissions.py`（权限校验）、`transfer_pages.py`（页面操作/路由）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
