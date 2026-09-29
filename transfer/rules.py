"""跨区域移交 - 移交规则层。

只表达纯业务规则（输入校验、状态机、版本判定），不读库、不做权限
判断、不接触 HTTP。数据以 dict 传入，规则冲突抛出 ``TransferRuleError``，
由页面操作层决定回滚与响应码。

规则要点：
- 发起：目标区域非空且不同于当前区域、原因必填、读取版本必须等于
  案例当前版本、已合并案例不能发起、已有未处理申请不能重复发起；
- 接受：申请必须仍为未处理状态；案例当前版本必须与申请时读取的
  版本一致（随访、医学审核等任何更新都会推进版本），否则接受被
  退回，防止旧申请覆盖最新审核结果；
- 拒绝：必须留下理由；
- 撤销：只有未处理申请可撤销。
"""
from __future__ import annotations

from typing import Any

from transfer.records import STATUS_PENDING


class TransferRuleError(Exception):
    """移交规则被违反。status 是建议的 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def validate_create(body: dict[str, Any]) -> tuple[str, str, int]:
    """校验发起申请的页面输入，返回 (目标区域, 原因, 读取版本)。"""
    target_region = str(body.get("target_region", "")).strip()
    reason = str(body.get("reason", "")).strip()
    read_revision = body.get("read_revision")
    if not target_region:
        raise TransferRuleError("target_region_required", "目标区域必填", 400)
    if not reason:
        raise TransferRuleError("reason_required", "移交原因必填", 400)
    if not isinstance(read_revision, int) or isinstance(read_revision, bool):
        raise TransferRuleError("read_revision_required", "读取版本 read_revision 必须是整数", 400)
    return target_region, reason, read_revision


def assert_can_create(
    case: dict[str, Any], target_region: str, read_revision: int, pending: dict[str, Any] | None
) -> None:
    if case["status"] == "merged":
        raise TransferRuleError("case_merged", "已合并案例不能发起跨区域移交")
    if target_region == case["region"]:
        raise TransferRuleError("same_region", "目标区域不能与当前区域相同", 400)
    if read_revision != case["revision"]:
        raise TransferRuleError("revision_conflict", "读取版本与案例当前版本不一致，请重新读取后再发起", 409)
    if pending is not None:
        raise TransferRuleError("transfer_pending", "该案例已有未处理的移交申请", 409)


def assert_can_accept(request: dict[str, Any], case: dict[str, Any]) -> None:
    if request["status"] != STATUS_PENDING:
        raise TransferRuleError("transfer_not_pending", "申请已处理，不能再次接受")
    if case["status"] == "merged":
        raise TransferRuleError("case_merged", "案例已合并，不能接受移交", 400)
    if case["revision"] != request["case_revision"]:
        raise TransferRuleError(
            "transfer_version_stale",
            "案例自申请后已被更新（随访/医学审核等），接受已退回，请重新核对后发起新申请",
            409,
        )
    if case["region"] != request["from_region"]:
        raise TransferRuleError("transfer_region_changed", "案例归属区域已变化，申请失效", 409)


def assert_can_reject(request: dict[str, Any], reason: str | None) -> None:
    if request["status"] != STATUS_PENDING:
        raise TransferRuleError("transfer_not_pending", "申请已处理，不能再拒绝")
    if not reason:
        raise TransferRuleError("reject_reason_required", "拒绝时必须填写理由", 400)


def assert_can_cancel(request: dict[str, Any]) -> None:
    if request["status"] != STATUS_PENDING:
        raise TransferRuleError("transfer_not_pending", "只能撤销未处理的移交申请")
