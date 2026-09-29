"""跨区域移交事务编排（协调规则 / 数据记录 / 权限三个独立模块）。

接受、拒绝、撤销均在单事务内完成：接受过程中任何一步出错都会整体回滚，
案例仍归原区域，绝不留下“半移交”状态。
"""
from __future__ import annotations

import sqlite3
from typing import Any

import transfer_permissions as permissions
import transfer_rules as rules
from errors import ApiError
from transfer_records import TransferStore, audit


class TransferService:
    def __init__(self, repo: Any):
        self.repo = repo
        self.store = TransferStore(repo.conn)

    def _case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise ApiError(404, "case_not_found", "案例不存在")
        return case

    def _transfer(self, transfer_id: int) -> dict[str, Any]:
        transfer = self.store.get(transfer_id)
        if not transfer:
            raise ApiError(404, "transfer_not_found", "移交申请不存在")
        return transfer

    def freeze_guard(self, case_id: int, action: str) -> None:
        """供随访/报告提交流程调用的等待期冻结闸口。"""
        rules.ensure_not_frozen(self.store.pending_for_case(case_id), action)

    def request_transfer(
        self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        target_region, reason, read_version = rules.validate_request_body(body)
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            permissions.assert_can_request(role, region, case)
            active = self.store.pending_for_case(case_id)
            rules.ensure_open_for_request(dict(case), active, target_region, read_version)
            try:
                transfer = self.store.insert(
                    conn,
                    case_id=case_id,
                    from_region=case["region"],
                    to_region=target_region,
                    reason=reason,
                    read_version=read_version,
                    requested_by=actor,
                    requested_region=region,
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "transfer_pending", "该案例已有待处理的移交申请") from exc
            audit(
                conn,
                case_id,
                actor,
                role,
                "region_transfer_requested",
                {"transfer_id": transfer["id"], "from_region": case["region"], "to_region": target_region,
                 "read_version": read_version, "reason": reason},
            )
            return transfer

    def respond_transfer(
        self, transfer_id: int, action: str, actor: str, role: str, region: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if action not in {"accept", "reject"}:
            raise ApiError(404, "not_found", "接口不存在")
        reject_reason = rules.validate_reject_reason(body) if action == "reject" else ""
        stale_reason = None
        with self.repo.tx() as conn:
            transfer = self.store.get(transfer_id)
            if not transfer:
                raise ApiError(404, "transfer_not_found", "移交申请不存在")
            permissions.assert_can_respond(role, region, transfer)
            rules.ensure_pending(transfer)
            case = self._case(conn, transfer["case_id"])

            if action == "reject":
                changed = self.store.close(
                    conn, transfer_id, status=rules.REJECTED,
                    responded_by=actor, response_reason=reject_reason,
                )
                if not changed:
                    raise ApiError(409, "transfer_closed", "申请已被处理")
                audit(
                    conn, case["id"], actor, role, "region_transfer_rejected",
                    {"transfer_id": transfer_id, "to_region": transfer["to_region"], "reason": reject_reason},
                )
            else:
                stale_reason = rules.accept_staleness(transfer, dict(case))
                if stale_reason is not None:
                    # 版本已变化：旧申请退回（置为 stale），案例归属保持不变，等待新申请
                    changed = self.store.close(
                        conn, transfer_id, status=rules.STALE,
                        responded_by=actor, closed_reason=stale_reason,
                    )
                    if not changed:
                        raise ApiError(409, "transfer_closed", "申请已被处理")
                    audit(
                        conn, case["id"], actor, role, "region_transfer_stale",
                        {"transfer_id": transfer_id, "closed_reason": stale_reason},
                    )
                else:
                    # 归属变更与申请关闭必须同一事务：任一步失败整体回滚，案例仍归原区域
                    self.store.change_case_region(conn, case["id"], transfer["to_region"])
                    changed = self.store.close(conn, transfer_id, status=rules.ACCEPTED, responded_by=actor)
                    if not changed:
                        raise ApiError(409, "transfer_closed", "申请已被处理")
                    audit(
                        conn, case["id"], actor, role, "region_transfer_accepted",
                        {"transfer_id": transfer_id, "from_region": transfer["from_region"],
                         "to_region": transfer["to_region"], "read_version": transfer["read_version"]},
                    )
            result = self.store.get(transfer_id)
            case_after = self._case(conn, transfer["case_id"])

        # 事务已提交后再返回冲突，确保 stale 状态已落库
        if stale_reason is not None:
            raise ApiError(409, "transfer_stale", stale_reason)
        return {"transfer": result, "case": dict(case_after)}

    def cancel_transfer(self, transfer_id: int, actor: str, role: str) -> dict[str, Any]:
        permissions.assert_can_cancel(role)
        with self.repo.tx() as conn:
            transfer = self.store.get(transfer_id)
            if not transfer:
                raise ApiError(404, "transfer_not_found", "移交申请不存在")
            rules.ensure_pending(transfer)
            changed = self.store.close(conn, transfer_id, status=rules.CANCELED, canceled_by=actor)
            if not changed:
                raise ApiError(409, "transfer_closed", "申请已被处理")
            audit(
                conn, transfer["case_id"], actor, role, "region_transfer_canceled",
                {"transfer_id": transfer_id},
            )
            return self.store.get(transfer_id)

    def get_transfer(self, transfer_id: int, role: str, region: str) -> dict[str, Any]:
        transfer = self._transfer(transfer_id)
        permissions.assert_can_view(role, region, transfer)
        return transfer

    def list_transfers(
        self, role: str, region: str, status: str | None = None, case_id: int | None = None
    ) -> list[dict[str, Any]]:
        if role == "reporter":
            raise ApiError(403, "transfer_forbidden", "无权查看移交记录")
        transfers = self.store.list(status=status)
        result = []
        for transfer in transfers:
            if case_id is not None and transfer["case_id"] != case_id:
                continue
            if role in {"global_admin", "medical_reviewer"}:
                result.append(transfer)
            elif region and region.upper() in {transfer["from_region"], transfer["to_region"]}:
                result.append(transfer)
        return result
