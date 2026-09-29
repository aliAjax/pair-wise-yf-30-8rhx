"""跨区域移交规则（纯领域规则，不接触数据库与 HTTP）。

规则独立维护：申请要素、受理条件、版本失效、等待期冻结都在此判定，
数据记录 / 权限校验 / 页面操作各自变化时不需要改动这里的判定逻辑。
"""
from __future__ import annotations

from typing import Any

from errors import ApiError

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
CANCELED = "canceled"
STALE = "stale"
TERMINAL_STATUSES = {ACCEPTED, REJECTED, CANCELED, STALE}

FREEZE_LABELS = {"followup": "随访", "report_submission": "报告提交"}


def normalize_region(value: Any) -> str:
    return str(value or "").strip().upper()


def validate_request_body(body: dict[str, Any]) -> tuple[str, str, int]:
    """校验原区域负责人填写的目标区域、原因、读取版本。"""
    target_region = normalize_region(body.get("target_region"))
    if not target_region:
        raise ApiError(400, "target_region_required", "目标区域必填")
    reason = str(body.get("reason", "")).strip()
    if not reason:
        raise ApiError(400, "transfer_reason_required", "移交原因必填")
    read_version = body.get("read_version")
    if not isinstance(read_version, int) or isinstance(read_version, bool) or read_version < 1:
        raise ApiError(400, "read_version_required", "读取版本必须是正整数")
    return target_region, reason, read_version


def validate_reject_reason(body: dict[str, Any]) -> str:
    reason = str(body.get("reason", "")).strip()
    if not reason:
        raise ApiError(400, "reject_reason_required", "拒绝移交必须填写理由")
    return reason


def ensure_open_for_request(
    case: Any, active_transfer: dict[str, Any] | None, target_region: str, read_version: int
) -> None:
    """发起申请前：案例可移交、目标区域不同、无在途申请、读取版本为最新。"""
    if case["status"] == "merged":
        raise ApiError(409, "case_merged", "已合并案例不能移交")
    if target_region == normalize_region(case["region"]):
        raise ApiError(400, "same_region", "目标区域不能与案例当前区域相同")
    if active_transfer is not None:
        raise ApiError(409, "transfer_pending", "该案例已有待处理的移交申请")
    if read_version != case["revision"]:
        raise ApiError(409, "revision_conflict", "案例版本已变化，请重新读取后再发起移交")


def ensure_pending(transfer: dict[str, Any]) -> None:
    if transfer["status"] != PENDING:
        raise ApiError(409, "transfer_closed", f"申请已处于 {transfer['status']} 状态，不能重复受理")


def accept_staleness(transfer: dict[str, Any], case: Any) -> str | None:
    """受理时返回失效原因；None 表示可以接受。

    申请基于发起时的读取版本，期间任何审核/随访造成版本推进，旧申请都要退回，
    不能覆盖最新审核结果。
    """
    if case["status"] == "merged":
        return "案例已合并，移交申请自动失效"
    if normalize_region(case["region"]) != normalize_region(transfer["from_region"]):
        return "案例归属区域已变化，移交申请失效"
    if case["revision"] != transfer["read_version"]:
        return (
            f"案例版本已变化（申请基于版本 {transfer['read_version']}，"
            f"当前版本 {case['revision']}），请原区域重新发起移交"
        )
    return None


def ensure_not_frozen(active_transfer: dict[str, Any] | None, action: str) -> None:
    """等待目标区域受理期间，暂停随访和报告提交。"""
    if active_transfer is not None:
        label = FREEZE_LABELS.get(action, action)
        raise ApiError(
            409,
            "transfer_pending",
            f"跨区域移交申请待目标区域受理，等待期间暂停{label}",
        )
