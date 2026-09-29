"""跨区域移交功能包。

按职责拆分为四个可独立维护的部分：

- ``transfer.records``     数据记录：移交申请表结构与读写；
- ``transfer.rules``       移交规则：发起、接受、拒绝、撤销的纯业务规则；
- ``transfer.permissions`` 权限校验：角色与区域的访问规则；
- ``transfer.actions``     页面操作：把页面动作编排成事务，供 HTTP 层调用。

注意：本包的 ``actions`` 层会惰性引用主应用的 ``app`` 模块，因此这里
不重新导出 ``TransferActions``，使用方应直接 ``from transfer.actions
import TransferActions``，避免包初始化阶段产生循环导入。
"""
from transfer.records import (
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TransferRecords,
)
from transfer.rules import TransferRuleError
from transfer.permissions import TransferPermissionError

__all__ = [
    "TransferRecords",
    "TransferRuleError",
    "TransferPermissionError",
    "STATUS_PENDING",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "STATUS_CANCELLED",
]
