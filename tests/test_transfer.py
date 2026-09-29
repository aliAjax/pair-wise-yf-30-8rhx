import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow
from transfer.records import TransferRecords
from transfer.rules import TransferRuleError
from transfer.permissions import TransferPermissionError

# 移交动作（actions 层）抛出域异常；主服务（随访/报告/查询）抛 ApiError
TRANSFER_ERRORS = (TransferRuleError, TransferPermissionError)


def _case_body(dedupe="intake", region="CN", **overrides):
    body = {
        "patient_ref": "P-1", "region": region, "product": "DrugA", "event_term": "肝损伤",
        "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False,
    }
    body.update(overrides)
    return body


class TransferFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = PharmacovigilanceService(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def case(self, dedupe="intake-1"):
        return self.svc.create_case("reporter-a", "reporter", "CN", _case_body(dedupe))["case"]

    def request(self, case, actor="lead-cn", read_revision=None, reason="录错区域，患者实际在欧盟"):
        return self.svc.transfers.request_transfer(
            case["id"], actor, "regional_lead", "CN",
            {"target_region": "EU", "reason": reason, "read_revision": read_revision or case["revision"]},
        )["transfer"]

    def test_request_accept_moves_ownership(self):
        case = self.case()
        transfer = self.request(case)
        self.assertEqual(transfer["status"], "pending")
        result = self.svc.transfers.accept(transfer["id"], "lead-eu", "regional_lead", "EU")
        self.assertEqual(result["case"]["region"], "EU")
        self.assertEqual(result["transfer"]["status"], "accepted")
        # 原区域负责人已无权访问，目标区域负责人可见
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_case(case["id"], "regional_lead", "CN")
        self.assertEqual(ctx.exception.status, 403)
        detail = self.svc.get_case(case["id"], "regional_lead", "EU")
        self.assertEqual(detail["case"]["region"], "EU")
        self.assertEqual(detail["transfers"][0]["to_region"], "EU")

    def test_only_source_region_lead_can_request(self):
        case = self.case()
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.request_transfer(
                case["id"], "lead-eu", "regional_lead", "EU",
                {"target_region": "EU", "reason": "x", "read_revision": 1},
            )
        self.assertEqual(ctx.exception.code, "transfer_request_forbidden")
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.request_transfer(
                case["id"], "reporter-a", "reporter", "CN",
                {"target_region": "EU", "reason": "x", "read_revision": 1},
            )
        self.assertEqual(ctx.exception.status, 403)

    def test_reject_requires_reason_and_keeps_ownership(self):
        case = self.case()
        transfer = self.request(case)
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.reject(transfer["id"], "lead-eu", "regional_lead", "EU", {"reason": "  "})
        self.assertEqual(ctx.exception.code, "reject_reason_required")
        result = self.svc.transfers.reject(
            transfer["id"], "lead-eu", "regional_lead", "EU", {"reason": "证据不足，仍属 CN"}
        )
        self.assertEqual(result["transfer"]["status"], "rejected")
        self.assertEqual(result["transfer"]["decision_reason"], "证据不足，仍属 CN")
        self.assertEqual(self.svc.get_case(case["id"], "regional_lead", "CN")["case"]["region"], "CN")
        # 拒绝后可重新发起
        again = self.request(case, reason="补充材料后重新申请")
        self.assertEqual(again["status"], "pending")

    def test_waiting_period_freezes_followups_and_report_submission(self):
        case = self.case()
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.request(case)
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(
                case["id"], "reporter-a", "reporter", "CN",
                {"content": "新进展", "source": "phone", "expected_revision": 1},
            )
        self.assertEqual(ctx.exception.code, "transfer_pending")
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "transfer_pending")
        # 医学审核不受等待期冻结，且审核推进版本
        reviewed = self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 1, "serious": True, "fatal": False, "causality": "related",
             "rationale": "住院记录", "received_at": iso(utcnow())},
        )
        self.assertEqual(reviewed["case"]["revision"], 2)

    def test_accept_after_version_change_is_returned(self):
        case = self.case()
        transfer = self.request(case)
        self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 1, "serious": True, "fatal": False, "causality": "related",
             "rationale": "住院记录", "received_at": iso(utcnow())},
        )
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.accept(transfer["id"], "lead-eu", "regional_lead", "EU")
        self.assertEqual(ctx.exception.code, "transfer_version_stale")
        # 接受被退回：申请仍未处理、归属仍是 CN，没有半移交
        stored = TransferRecords(self.svc.repo.conn).get(transfer["id"])
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(self.svc.get_case(case["id"], "regional_lead", "CN")["case"]["region"], "CN")
        # 目标区域可拒绝并留理由收尾
        rejected = self.svc.transfers.reject(
            transfer["id"], "lead-eu", "regional_lead", "EU", {"reason": "版本已变化，请重新发起"}
        )
        self.assertEqual(rejected["transfer"]["status"], "rejected")

    def test_only_target_region_lead_can_decide(self):
        case = self.case()
        transfer = self.request(case)
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.accept(transfer["id"], "lead-cn", "regional_lead", "CN")
        self.assertEqual(ctx.exception.code, "transfer_decision_forbidden")
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.reject(transfer["id"], "admin", "global_admin", "", {"reason": "x"})
        self.assertEqual(ctx.exception.code, "transfer_decision_forbidden")

    def test_global_admin_cancels_pending_only(self):
        case = self.case()
        transfer = self.request(case)
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.cancel(transfer["id"], "lead-cn", "regional_lead")
        self.assertEqual(ctx.exception.code, "transfer_cancel_forbidden")
        result = self.svc.transfers.cancel(transfer["id"], "admin", "global_admin")
        self.assertEqual(result["transfer"]["status"], "cancelled")
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.cancel(transfer["id"], "admin", "global_admin")
        self.assertEqual(ctx.exception.code, "transfer_not_pending")
        # 撤销后随访恢复，且可重新发起
        self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "恢复随访", "source": "phone", "expected_revision": 1},
        )
        self.request(self.svc.get_case(case["id"], "regional_lead", "CN")["case"], reason="再次申请")

    def test_duplicate_and_stale_create_rejected(self):
        case = self.case()
        self.request(case)
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.request(case)
        self.assertEqual(ctx.exception.code, "transfer_pending")
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.request_transfer(
                case["id"], "lead-cn", "regional_lead", "CN",
                {"target_region": "EU", "reason": "x", "read_revision": 99},
            )
        self.assertEqual(ctx.exception.code, "revision_conflict")
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.request_transfer(
                case["id"], "lead-cn", "regional_lead", "CN",
                {"target_region": "CN", "reason": "x", "read_revision": 1},
            )
        self.assertEqual(ctx.exception.code, "same_region")

    def test_history_visible_after_upgrade_and_transfer(self):
        # 直接造一个只有旧版 cases 表的库（无移交表、无其他新表）
        old_path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(old_path)
        conn.execute(
            """CREATE TABLE cases (id INTEGER PRIMARY KEY AUTOINCREMENT, case_no TEXT UNIQUE, patient_ref TEXT,
               region TEXT, product TEXT, event_term TEXT, onset_at TEXT, received_at TEXT,
               serious INTEGER, fatal INTEGER, causality TEXT, report_due_at TEXT, status TEXT,
               revision INTEGER, merged_into INTEGER, created_by TEXT, created_at TEXT, updated_at TEXT)"""
        )
        now = iso(utcnow())
        conn.execute(
            """INSERT INTO cases(case_no,patient_ref,region,product,event_term,received_at,serious,fatal,
               report_due_at,status,revision,created_by,created_at,updated_at)
               VALUES('PV-OLD-1','P-OLD','CN','DrugA','皮疹',?,0,0,?,'open',1,'reporter-a',?,?)""",
            (now, now, now, now),
        )
        conn.commit()
        conn.close()
        # 升级：补装移交表后，旧案例与历史继续可查
        svc = PharmacovigilanceService(old_path)
        old = svc.get_case(1, "regional_lead", "CN")
        self.assertEqual(old["case"]["case_no"], "PV-OLD-1")
        transfer = svc.transfers.request_transfer(
            1, "lead-cn", "regional_lead", "CN",
            {"target_region": "US", "reason": "历史录错区域", "read_revision": 1},
        )["transfer"]
        svc.transfers.accept(transfer["id"], "lead-us", "regional_lead", "US")
        moved = svc.get_case(1, "global_admin", "")
        self.assertEqual(moved["case"]["region"], "US")
        self.assertEqual(moved["case"]["case_no"], "PV-OLD-1")
        self.assertEqual(moved["transfers"][0]["from_region"], "CN")
        actions = [row["action"] for row in svc.repo.conn.execute("SELECT action FROM audit_log WHERE case_id=1")]
        self.assertIn("transfer_requested", actions)
        self.assertIn("transfer_accepted", actions)

    def test_list_visibility_by_region_and_role(self):
        c1 = self.case("intake-a")
        t1 = self.request(c1)
        self.svc.create_case("reporter-us", "reporter", "US",
                             _case_body("intake-b", region="US", patient_ref="P-2"))
        t2 = self.svc.transfers.request_transfer(
            2, "lead-us", "regional_lead", "US",
            {"target_region": "CN", "reason": "应归 CN", "read_revision": 1},
        )["transfer"]
        cn = self.svc.transfers.list_requests("regional_lead", "CN", {})["transfers"]
        self.assertEqual({t["id"] for t in cn}, {t1["id"], t2["id"]})
        us = self.svc.transfers.list_requests("regional_lead", "US", {})["transfers"]
        self.assertEqual({t["id"] for t in us}, {t2["id"]})
        eu = self.svc.transfers.list_requests("regional_lead", "EU", {})["transfers"]
        self.assertEqual({t["id"] for t in eu}, {t1["id"]})
        admin = self.svc.transfers.list_requests("global_admin", "", {})["transfers"]
        self.assertEqual({t["id"] for t in admin}, {t1["id"], t2["id"]})
        with self.assertRaises(TRANSFER_ERRORS) as ctx:
            self.svc.transfers.list_requests("reporter", "CN", {})
        self.assertEqual(ctx.exception.status, 403)
        pending = self.svc.transfers.list_requests("global_admin", "", {"status": ["pending"]})["transfers"]
        self.assertEqual(len(pending), 2)


if __name__ == "__main__":
    unittest.main()
