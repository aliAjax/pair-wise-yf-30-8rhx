"""跨区域移交 - 权限校验层。

只判断角色与区域是否有权执行某个移交动作，不含业务规则，也不读库。
传入的是已查出的案例/申请 dict，校验失败抛出 ``TransferPermissionError``。

权限模型：
- 发起移交：仅原区域负责人（regional_lead 且 X-Region 与案例当前区域一致）；
- 接受/拒绝：仅目标区域负责人（regional_lead 且 X-Region 与申请目标区域一致）；
- 撤销未处理申请：仅全局管理员（global_admin）；
- 查看申请列表：全局管理员和医学审核员可见全部；区域负责人仅可见
  与本区域相关（原区域或目标区域）的申请；上报员不可见。
"""
from __future__ import annotations

from typing import Any, Sequence


class TransferPermissionError(Exception):
    """移交动作的权限校验失败，建议 HTTP 403。"""

    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def can_request(case: dict[str, Any], role: str, region: str) -> None:
    if role != "regional_lead" or region != case["region"]:
        raise TransferPermissionError(
            "transfer_request_forbidden", "只有原区域负责人可以发起跨区域移交"
        )


def can_decide(request: dict[str, Any], role: str, region: str) -> None:
    if role != "regional_lead" or region != request["to_region"]:
        raise TransferPermissionError(
            "transfer_decision_forbidden", "只有目标区域负责人可以接受或拒绝移交申请"
        )


def can_cancel(role: str) -> None:
    if role != "global_admin":
        raise TransferPermissionError("transfer_cancel_forbidden", "只有全局管理员可以撤销移交申请")


def can_view_all(role: str) -> bool:
    return role in {"global_admin", "medical_reviewer"}


def visible_regions(role: str, region: str) -> Sequence[str] | None:
    """列表过滤参数：None 表示可见全部，否则返回本区域过滤值。"""
    if can_view_all(role):
        return None
    if role == "regional_lead":
        return [region]
    raise TransferPermissionError("transfer_list_forbidden", "当前角色不能查看移交申请")


def can_view_case_transfers(role: str, region: str, case: dict[str, Any]) -> bool:
    return can_view_all(role) or (role == "regional_lead" and region == case["region"])
