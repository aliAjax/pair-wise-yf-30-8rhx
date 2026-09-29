"""跨区域移交 - 数据记录层。

只负责 ``transfer_requests`` 表的结构升级和读写，不包含任何业务规则、
权限判断或 HTTP 逻辑。表结构采用 ``CREATE TABLE IF NOT EXISTS`` 的增量
升级方式，不改动既有案例表，旧案例的随访、报告、审核与审计历史在升级
后原样保留、继续可查。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

# 移交申请状态
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUSES = {STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_CANCELLED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    from_region TEXT NOT NULL,
    to_region TEXT NOT NULL,
    reason TEXT NOT NULL,
    case_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
/* 同一案例同时只允许存在一份未处理申请 */
CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_pending_case
    ON transfer_requests(case_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_transfer_case ON transfer_requests(case_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TransferRecords:
    """移交申请的数据记录访问对象，绑定一个 SQLite 连接。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def install(self) -> None:
        """在既有库上增量安装移交申请表（升级旧库，不触碰历史数据）。"""
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def add(
        self,
        *,
        case_id: int,
        from_region: str,
        to_region: str,
        reason: str,
        case_revision: int,
        requested_by: str,
        now: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO transfer_requests(case_id,from_region,to_region,reason,case_revision,
               status,requested_by,requested_at,created_at,updated_at)
               VALUES(?,?,?,?,?,'pending',?,?,?,?)""",
            (case_id, from_region, to_region, reason, case_revision, requested_by, now, now, now),
        )
        return int(cur.lastrowid)

    def get(self, request_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM transfer_requests WHERE id=?", (request_id,)).fetchone()
        return self._dict(row)

    def pending_for_case(self, case_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM transfer_requests WHERE case_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return self._dict(row)

    def list_for_case(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM transfer_requests WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all(self, regions: Sequence[str] | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        args: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            args.append(status)
        if regions is not None:
            placeholders = ",".join("?" for _ in regions)
            clauses.append(f"(from_region IN ({placeholders}) OR to_region IN ({placeholders}))")
            args.extend([*regions, *regions])
        sql = "SELECT * FROM transfer_requests WHERE " + " AND ".join(clauses) + " ORDER BY id DESC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def decide(
        self,
        request_id: int,
        status: str,
        decided_by: str,
        decision_reason: str | None,
        now: str,
    ) -> None:
        self.conn.execute(
            """UPDATE transfer_requests
               SET status=?,decided_by=?,decided_at=?,decision_reason=?,updated_at=?
               WHERE id=?""",
            (status, decided_by, now, decision_reason, now, request_id),
        )
