"""跨区域移交 - 页面操作层。

把页面上的每个动作（发起、接受、拒绝、撤销、列表/详情查询）编排成
一次完整的数据库事务，组合数据记录、移交规则与权限校验三个独立部分。
本层只抛出域异常（``TransferRuleError`` / ``TransferPermissionError``），
不依赖 HTTP 层；由调用方（HTTP 处理器）统一翻译成响应码。

接受动作在同一个事务里完成"申请置为已接受 + 案例归属切换 + 审计落库"，
任何一步规则校验失败都在写入前回滚；运行期异常同样整体回滚，案例
仍归原区域，不会留下半移交状态。等待期间的随访/报告冻结由主服务
在各自事务内调用 ``pending_for_case`` 实现。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from transfer import permissions
from transfer.records import (
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_REJECTED,
    TransferRecords,
    now_iso,
)
from transfer.permissions import TransferPermissionError
from transfer.rules import (
    TransferRuleError,
    assert_can_accept,
    assert_can_cancel,
    assert_can_create,
    assert_can_reject,
    validate_create,
)

AuditFn = Callable[[Any, int | None, str, str, str, dict[str, Any]], None]


def _default_audit(conn: Any, case_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
    """脱离主应用时的审计兜底写法，结构与核心 Repository.audit 保持一致。"""
    conn.execute(
        "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
        (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now_iso()),
    )


class TransferActions:
    def __init__(self, service: Any, audit: AuditFn | None = None):
        self.service = service
        self.repo = service.repo
        self._audit = audit or _default_audit

    def _records(self, conn: Any) -> TransferRecords:
        return TransferRecords(conn)

    def _case(self, conn: Any, case_id: int) -> dict[str, Any]:
        return dict(self.service._case(conn, case_id))

    def _request(self, records: TransferRecords, request_id: int) -> dict[str, Any]:
        request = records.get(request_id)
        if request is None:
            raise TransferRuleError("transfer_not_found", "移交申请不存在", 404)
        return request

    def request_transfer(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        target_region, reason, read_revision = validate_create(body)
        with self.repo.tx() as conn:
            records = self._records(conn)
            case = self._case(conn, case_id)
            permissions.can_request(case, role, region)
            pending = records.pending_for_case(case_id)
            assert_can_create(case, target_region, read_revision, pending)
            now = now_iso()
            try:
                request_id = records.add(
                    case_id=case_id,
                    from_region=case["region"],
                    to_region=target_region,
                    reason=reason,
                    case_revision=read_revision,
                    requested_by=actor,
                    now=now,
                )
            except sqlite3.IntegrityError as exc:
                # 并发下部分唯一索引兜底：同一案例已有 pending 申请
                raise TransferRuleError("transfer_pending", "该案例已有未处理的移交申请", 409) from exc
            self._audit(conn, case_id, actor, role, "transfer_requested", {
                "transfer_id": request_id, "to_region": target_region,
                "read_revision": read_revision, "reason": reason,
            })
            return {"transfer": records.get(request_id)}

    def accept(self, request_id: int, actor: str, role: str, region: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            records = self._records(conn)
            request = self._request(records, request_id)
            permissions.can_decide(request, role, region)
            case = self._case(conn, request["case_id"])
            assert_can_accept(request, case)
            now = now_iso()
            # 接受、归属切换、审计在同一事务内提交，失败则整笔回滚
            records.decide(request_id, STATUS_ACCEPTED, actor, None, now)
            conn.execute(
                "UPDATE cases SET region=?,updated_at=? WHERE id=?",
                (request["to_region"], now, case["id"]),
            )
            self._audit(conn, case["id"], actor, role, "transfer_accepted", {
                "transfer_id": request_id, "from_region": request["from_region"],
                "to_region": request["to_region"], "case_revision": case["revision"],
            })
            return {
                "transfer": records.get(request_id),
                "case": dict(conn.execute("SELECT * FROM cases WHERE id=?", (case["id"],)).fetchone()),
            }

    def reject(self, request_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        with self.repo.tx() as conn:
            records = self._records(conn)
            request = self._request(records, request_id)
            permissions.can_decide(request, role, region)
            assert_can_reject(request, reason)
            now = now_iso()
            records.decide(request_id, STATUS_REJECTED, actor, reason, now)
            self._audit(conn, request["case_id"], actor, role, "transfer_rejected", {
                "transfer_id": request_id, "reason": reason,
            })
            return {"transfer": records.get(request_id)}

    def cancel(self, request_id: int, actor: str, role: str) -> dict[str, Any]:
        permissions.can_cancel(role)
        with self.repo.tx() as conn:
            records = self._records(conn)
            request = self._request(records, request_id)
            assert_can_cancel(request)
            now = now_iso()
            records.decide(request_id, STATUS_CANCELLED, actor, None, now)
            self._audit(conn, request["case_id"], actor, role, "transfer_cancelled", {
                "transfer_id": request_id,
            })
            return {"transfer": records.get(request_id)}

    def list_requests(self, role: str, region: str, query: dict[str, list[str]]) -> dict[str, Any]:
        status = query.get("status", [None])[0]
        if status is not None:
            status = str(status).strip()
        regions = permissions.visible_regions(role, region)
        transfers = self._records(self.repo.conn).list_all(regions=regions, status=status)
        return {"transfers": transfers}

    def get_request(self, request_id: int, role: str, region: str) -> dict[str, Any]:
        conn = self.repo.conn
        records = self._records(conn)
        request = self._request(records, request_id)
        case = self._case(conn, request["case_id"])
        if not permissions.can_view_all(role) and not (
            role == "regional_lead" and region in (request["from_region"], request["to_region"])
        ):
            raise TransferPermissionError("transfer_forbidden", "无权查看该移交申请")
        # 区域负责人只能看到申请涉及区域时的流程记录；案例已移出本区域后，
        # 不再返回案例完整内容，只给归属状态摘要
        if role == "regional_lead" and case["region"] != region:
            case_summary = {"id": case["id"], "region": case["region"], "status": case["status"], "revision": case["revision"]}
            return {"transfer": request, "case": case_summary}
        return {"transfer": request, "case": case}

    def case_transfers(self, conn: Any, case_id: int) -> list[dict[str, Any]]:
        """供案例详情在同一连接内附带移交记录（查看权限由主服务把关）。"""
        return self._records(conn).list_for_case(case_id)

    def pending_for_case(self, conn: Any, case_id: int) -> dict[str, Any] | None:
        """供随访/报告动作在自身事务内判断等待期冻结。"""
        return self._records(conn).pending_for_case(case_id)
