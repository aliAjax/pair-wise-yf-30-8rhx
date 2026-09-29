"""跨区域移交页面操作（独立维护：仅负责 HTTP 路由与参数解析）。

规则、数据、权限的变化不影响路由注册；路由本身的增删也不触碰其他模块。
"""
from __future__ import annotations

from typing import Any


def register(api: Any) -> None:
    """在 Handler 上挂载跨区域移交相关的 GET/POST 分发函数。"""
    svc = api.service.transfers

    def dispatch_get(handler, path: str, query: dict[str, list[str]]) -> Any:
        actor, role, region = api.service.identity(handler.headers)
        if path == "/api/transfers":
            status = query["status"][0] if query.get("status") else None
            case_id = None
            if query.get("case_id"):
                if not query["case_id"][0].isdigit():
                    from errors import ApiError
                    raise ApiError(400, "invalid_case_id", "case_id 必须是整数")
                case_id = int(query["case_id"][0])
            return 200, {"transfers": svc.list_transfers(role, region, status=status, case_id=case_id)}
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "transfers"] and parts[2].isdigit():
            return 200, {"transfer": svc.get_transfer(int(parts[2]), role, region)}
        return None

    def dispatch_post(handler, path: str, body: dict[str, Any]) -> Any:
        actor, role, region = api.service.identity(handler.headers)
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[2].isdigit() and parts[3] == "transfers":
            return 201, svc.request_transfer(int(parts[2]), actor, role, region, body)
        if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[2].isdigit():
            transfer_id, action = int(parts[2]), parts[3]
            if action in {"accept", "reject"}:
                return 200, svc.respond_transfer(transfer_id, action, actor, role, region, body)
            if action == "cancel":
                return 200, {"transfer": svc.cancel_transfer(transfer_id, actor, role)}
        return None

    api.transfer_get = dispatch_get
    api.transfer_post = dispatch_post
