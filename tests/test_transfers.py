"""跨区域移交流程测试：规则、数据记录、权限校验、冻结、版本失效与原子性。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, Repository, iso, utcnow  # noqa: E402
from transfer_rules import PENDING, ACCEPTED, REJECTED, CANCELED, STALE  # noqa: E402


class TransferFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.svc = PharmacovigilanceService(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def create_case(self, dedupe="xfer-1", region="CN"):
        return self.svc.create_case(
            "reporter-a", "reporter", region,
            {"patient_ref": "P-1", "region": region, "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow())},
        )["case"]

    def request(self, case_id, role="regional_lead", region="CN", body=None):
        body = body or {"target_region": "US", "reason": "录入时错选区域", "read_version": 1}
        return self.svc.transfers.request_transfer(case_id, f"lead-{region.lower()}", role, region, body)

    # 1. 完整接受流程：归属切换、状态落库、随访/报告恢复可用
    def test_accept_changes_ownership_and_unfreezes(self):
        case = self.create_case()
        transfer = self.request(case["id"])
        self.assertEqual(transfer["status"], PENDING)

        # 等待期间暂停随访
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "新信息", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "transfer_pending")

        result = self.svc.transfers.respond_transfer(
            transfer["id"], "accept", "lead-us", "regional_lead", "US", {})
        self.assertEqual(result["case"]["region"], "US")
        self.assertEqual(result["transfer"]["status"], ACCEPTED)

        # 原区域失去访问权，新区域可以继续随访
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_case(case["id"], "regional_lead", "CN")
        self.assertEqual(ctx.exception.status, 403)
        followup = self.svc.add_followup(case["id"], "reporter-us", "reporter", "US",
                                         {"content": "移交后随访", "source": "email",
                                          "expected_revision": 1})
        self.assertEqual(followup["revision"], 2)

        # 历史（含移交记录与审计）继续可查
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["transfers"]), 1)
        actions = {a["action"] for a in detail["audit"]}
        self.assertIn("region_transfer_requested", actions)
        self.assertIn("region_transfer_accepted", actions)

    # 2. 拒绝必须留理由，案例仍归原区域
    def test_reject_requires_reason_and_keeps_region(self):
        case = self.create_case("xfer-2")
        transfer = self.request(case["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.respond_transfer(
                transfer["id"], "reject", "lead-us", "regional_lead", "US", {})
        self.assertEqual(ctx.exception.code, "reject_reason_required")

        result = self.svc.transfers.respond_transfer(
            transfer["id"], "reject", "lead-us", "regional_lead", "US",
            {"reason": "产品未在 US 上市，材料不完整"})
        self.assertEqual(result["transfer"]["status"], REJECTED)
        self.assertEqual(result["case"]["region"], "CN")
        self.assertIsNotNone(result["transfer"]["responded_at"])

        # 拒绝后不再冻结，原区域可继续随访
        followup = self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                         {"content": "继续跟进", "source": "email",
                                          "expected_revision": 1})
        self.assertEqual(followup["revision"], 2)

    # 3. 等待期间报告提交被冻结
    def test_report_submission_frozen_while_pending(self):
        case = self.create_case("xfer-3")
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.request(case["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "transfer_pending")

    # 4. 申请后案例版本变化（医学审核），接受必须退回旧申请，不能覆盖最新审核
    def test_accept_after_version_change_is_rejected_as_stale(self):
        case = self.create_case("xfer-4")
        transfer = self.request(case["id"])
        self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 1, "serious": True, "fatal": False, "causality": "related",
             "rationale": "住院记录支持严重性升级", "received_at": iso(utcnow())})
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.respond_transfer(
                transfer["id"], "accept", "lead-us", "regional_lead", "US", {})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "transfer_stale")

        stored = self.svc.transfers.get_transfer(transfer["id"], "global_admin", "")
        self.assertEqual(stored["status"], STALE)
        self.assertIn("版本", stored["closed_reason"])
        # 案例仍归原区域，最新审核保留
        fresh = self.svc.get_case(case["id"], "global_admin", "")["case"]
        self.assertEqual(fresh["region"], "CN")
        self.assertEqual(fresh["revision"], 2)
        self.assertEqual(fresh["serious"], 1)

        # 失效后可按新版本重新发起并接受
        again = self.request(case["id"], body={"target_region": "US", "reason": "重新发起",
                                               "read_version": 2})
        accepted = self.svc.transfers.respond_transfer(
            again["id"], "accept", "lead-us", "regional_lead", "US", {})
        self.assertEqual(accepted["case"]["region"], "US")

    # 5. 接受过程出错整体回滚：不留下半移交状态
    def test_accept_failure_rolls_back_region_and_transfer(self):
        case = self.create_case("xfer-5")
        transfer = self.request(case["id"])
        import transfer_service
        with patch.object(transfer_service, "audit",
                          side_effect=RuntimeError("audit sink down")):
            with self.assertRaises(RuntimeError):
                self.svc.transfers.respond_transfer(
                    transfer["id"], "accept", "lead-us", "regional_lead", "US", {})
        fresh = self.svc.get_case(case["id"], "global_admin", "")["case"]
        self.assertEqual(fresh["region"], "CN")
        stored = self.svc.transfers.get_transfer(transfer["id"], "global_admin", "")
        self.assertEqual(stored["status"], PENDING)

        # 服务仍可正常受理该申请（不存在半移交）
        accepted = self.svc.transfers.respond_transfer(
            transfer["id"], "accept", "lead-us", "regional_lead", "US", {})
        self.assertEqual(accepted["case"]["region"], "US")

    # 6. 全局管理员可撤销未处理申请；已终结申请不可撤销
    def test_global_admin_cancels_pending_only(self):
        case = self.create_case("xfer-6")
        transfer = self.request(case["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.cancel_transfer(transfer["id"], "lead-cn", "regional_lead")
        self.assertEqual(ctx.exception.status, 403)

        canceled = self.svc.transfers.cancel_transfer(transfer["id"], "admin-1", "global_admin")
        self.assertEqual(canceled["status"], CANCELED)
        self.assertEqual(canceled["canceled_by"], "admin-1")
        # 撤销后冻结解除
        followup = self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                         {"content": "继续", "source": "email",
                                          "expected_revision": 1})
        self.assertEqual(followup["revision"], 2)

        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.cancel_transfer(transfer["id"], "admin-1", "global_admin")
        self.assertEqual(ctx.exception.code, "transfer_closed")

    # 7. 权限校验：只有原区域负责人能发起，只有目标区域负责人能受理
    def test_permission_checks(self):
        case = self.create_case("xfer-7")
        with self.assertRaises(ApiError) as ctx:
            self.request(case["id"], role="reporter", region="CN")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.request(case["id"], role="regional_lead", region="US")
        self.assertEqual(ctx.exception.status, 403)

        transfer = self.request(case["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.respond_transfer(
                transfer["id"], "accept", "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "transfer_target_forbidden")

        # reporter 不能查看移交记录
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.get_transfer(transfer["id"], "reporter", "CN")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.list_transfers("reporter", "CN")
        self.assertEqual(ctx.exception.status, 403)
        # 但 reporter 查看自己区域案例详情不受影响（只是不返回移交记录）
        detail = self.svc.get_case(case["id"], "reporter", "CN")
        self.assertEqual(detail["transfers"], [])

    # 8. 发起校验：目标区域、原因、读取版本；同案例在途申请唯一
    def test_request_validation_and_single_pending(self):
        case = self.create_case("xfer-8")
        for bad in [{"target_region": "", "reason": "x", "read_version": 1},
                    {"target_region": "US", "reason": "", "read_version": 1},
                    {"target_region": "US", "reason": "x", "read_version": "1"},
                    {"target_region": "cn", "reason": "x", "read_version": 1}]:
            with self.assertRaises(ApiError):
                self.request(case["id"], body=bad)
        self.request(case["id"])
        with self.assertRaises(ApiError) as ctx:
            self.request(case["id"])
        self.assertEqual(ctx.exception.code, "transfer_pending")
        # 过期读取版本不能发起
        self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 1, "serious": True, "fatal": False, "causality": "related",
             "rationale": "x", "received_at": iso(utcnow())})
        pending = self.svc.transfers.store.pending_for_case(case["id"])
        self.svc.transfers.cancel_transfer(pending["id"], "admin", "global_admin")
        with self.assertRaises(ApiError) as ctx:
            self.request(case["id"], body={"target_region": "US", "reason": "x", "read_version": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")

    # 9. 已终结申请不能重复受理
    def test_terminal_transfer_cannot_be_processed_again(self):
        case = self.create_case("xfer-9")
        transfer = self.request(case["id"])
        self.svc.transfers.respond_transfer(
            transfer["id"], "reject", "lead-us", "regional_lead", "US", {"reason": "no"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.transfers.respond_transfer(
                transfer["id"], "accept", "lead-us", "regional_lead", "US", {})
        self.assertEqual(ctx.exception.code, "transfer_closed")

    # 10. 旧库升级：既有案例与历史在追加新表后继续可查
    def test_schema_upgrade_preserves_history(self):
        old = PharmacovigilanceService(self.db_path)
        case = old.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-9", "region": "CN", "product": "DrugB", "event_term": "发热",
             "source": "email", "dedupe_key": "legacy-1", "received_at": iso(utcnow())},
        )["case"]
        old.add_followup(case["id"], "reporter-a", "reporter", "CN",
                         {"content": "历史随访", "source": "phone", "expected_revision": 1})
        del old

        # 以新结构重新打开同一数据库（模拟升级重启）
        upgraded = PharmacovigilanceService(self.db_path)
        detail = upgraded.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["case"]["product"], "DrugB")
        self.assertEqual(len(detail["followups"]), 1)
        self.assertEqual(detail["transfers"], [])
        # 新功能在旧库上直接可用
        transfer = upgraded.transfers.request_transfer(
            case["id"], "lead-cn", "regional_lead", "CN",
            {"target_region": "EU", "reason": "错录", "read_version": 2})
        self.assertEqual(transfer["status"], PENDING)


if __name__ == "__main__":
    unittest.main()
