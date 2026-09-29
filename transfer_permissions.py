"""跨区域移交权限校验（独立维护，只做身份与归属判定）。"""
from __future__ import annotations

from typing import Any

from errors import ApiError


def _same_region(left: Any, right: Any) -> bool:
    return str(left or "").strip().upper() == str(right or "").strip().upper()


def assert_can_request(role: str, region: str, case: Any) -> None:
    """仅原区域负责人可以发起移交。"""
    if role != "regional_lead":
        raise ApiError(403, "transfer_forbidden", "只有原区域负责人可以发起跨区域移交")
    if not _same_region(region, case["region"]):
        raise ApiError(403, "region_forbidden", "只能对本区域案例发起移交")


def assert_can_respond(role: str, region: str, transfer: dict[str, Any]) -> None:
    """仅目标区域负责人可以接受或拒绝。"""
    if role != "regional_lead":
        raise ApiError(403, "transfer_forbidden", "只有目标区域负责人可以受理移交申请")
    if not _same_region(region, transfer["to_region"]):
        raise ApiError(403, "transfer_target_forbidden", "只有目标区域负责人可以受理该申请")


def assert_can_cancel(role: str) -> None:
    """仅全局管理员可以撤销未处理申请。"""
    if role != "global_admin":
        raise ApiError(403, "transfer_cancel_forbidden", "只有全局管理员可以撤销移交申请")


def assert_can_view(role: str, region: str, transfer: dict[str, Any]) -> None:
    if role == "reporter":
        raise ApiError(403, "transfer_forbidden", "移交记录仅对区域负责人、医学审核员和全局管理员开放")
    if role in {"global_admin", "medical_reviewer"}:
        return
    if _same_region(region, transfer["from_region"]) or _same_region(region, transfer["to_region"]):
        return
    raise ApiError(403, "transfer_forbidden", "无权查看该移交申请")
