"""跨区域移交数据记录（独立维护：表结构、索引、读写 SQL）。

新增 region_transfers 表，不改动 cases 等既有表，旧库启动时自动建表，
升级前的案例与历史记录原样保留、继续可查。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable


def _iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS region_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    from_region TEXT NOT NULL,
    to_region TEXT NOT NULL,
    reason TEXT NOT NULL,
    read_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL,
    requested_region TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    responded_by TEXT,
    responded_at TEXT,
    response_reason TEXT,
    canceled_by TEXT,
    canceled_at TEXT,
    closed_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_region_transfers_case ON region_transfers(case_id);
CREATE INDEX IF NOT EXISTS idx_region_transfers_status ON region_transfers(status);
-- 同一案例同时最多只有一条待处理申请（数据库层兜底）
CREATE UNIQUE INDEX IF NOT EXISTS idx_region_transfers_one_pending
    ON region_transfers(case_id) WHERE status='pending';
"""

# 申请状态：pending / accepted / rejected / canceled / stale
_COLUMNS = (
    "id,case_id,from_region,to_region,reason,read_version,status,requested_by,"
    "requested_region,requested_at,responded_by,responded_at,response_reason,"
    "canceled_by,canceled_at,closed_reason"
)


def audit(
    conn: sqlite3.Connection,
    case_id: int,
    actor: str,
    role: str,
    action: str,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
        (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), _iso()),
    )


class TransferStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _many(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(r) for r in rows]

    def get(self, transfer_id: int) -> dict[str, Any] | None:
        return self._one(
            self.conn.execute(f"SELECT {_COLUMNS} FROM region_transfers WHERE id=?", (transfer_id,)).fetchone()
        )

    def pending_for_case(self, case_id: int) -> dict[str, Any] | None:
        return self._one(
            self.conn.execute(
                f"SELECT {_COLUMNS} FROM region_transfers WHERE case_id=? AND status='pending'",
                (case_id,),
            ).fetchone()
        )

    def list_for_case(self, case_id: int) -> list[dict[str, Any]]:
        return self._many(
            self.conn.execute(
                f"SELECT {_COLUMNS} FROM region_transfers WHERE case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        )

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM region_transfers WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(f"SELECT {_COLUMNS} FROM region_transfers ORDER BY id").fetchall()
        return self._many(rows)

    def insert(
        self,
        conn: sqlite3.Connection,
        *,
        case_id: int,
        from_region: str,
        to_region: str,
        reason: str,
        read_version: int,
        requested_by: str,
        requested_region: str,
    ) -> dict[str, Any]:
        now = _iso()
        cursor = conn.execute(
            """INSERT INTO region_transfers(case_id,from_region,to_region,reason,read_version,
               requested_by,requested_region,requested_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (case_id, from_region, to_region, reason, read_version, requested_by, requested_region, now),
        )
        return self.get(cursor.lastrowid)  # type: ignore[arg-type]

    def close(
        self,
        conn: sqlite3.Connection,
        transfer_id: int,
        *,
        status: str,
        responded_by: str | None = None,
        response_reason: str | None = None,
        canceled_by: str | None = None,
        closed_reason: str | None = None,
    ) -> int:
        cursor = conn.execute(
            """UPDATE region_transfers SET status=?, responded_by=COALESCE(?,responded_by),
               responded_at=CASE WHEN ? IS NOT NULL THEN ? ELSE responded_at END,
               response_reason=COALESCE(?,response_reason),
               canceled_by=COALESCE(?,canceled_by),
               canceled_at=CASE WHEN ? IS NOT NULL THEN ? ELSE canceled_at END,
               closed_reason=COALESCE(?,closed_reason)
               WHERE id=? AND status='pending'""",
            (
                status,
                responded_by,
                responded_by,
                _iso() if responded_by else None,
                response_reason,
                canceled_by,
                canceled_by,
                _iso() if canceled_by else None,
                closed_reason,
                transfer_id,
            ),
        )
        return cursor.rowcount

    @staticmethod
    def change_case_region(conn: sqlite3.Connection, case_id: int, to_region: str) -> None:
        conn.execute(
            "UPDATE cases SET region=?,updated_at=? WHERE id=?", (to_region, _iso(), case_id)
        )
