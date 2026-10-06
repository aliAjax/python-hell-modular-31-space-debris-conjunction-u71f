import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


def make_event(service, primary="SAT-1", secondary="DEB-9", tca="2026-09-28T12:00:00+00:00",
               distance=120, covariance=100, hours=18, actor="analyst-1"):
    item = service.create_item({
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "fuel_budget_m_s": 5,
        "track_age_hours": 1,
        "operating_organizations": ["Org-A", "Org-B"],
    }, actor, "analyst")
    item = service.act(item["id"], "assess", {"hours_to_tca": hours}, actor, "analyst", item["version"])
    return item


WINDOW_A = "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z"
WINDOW_B = "2026-09-28T10:00:00Z/2026-09-28T11:00:00Z"


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_event_command_receipt_chain_success(self):
        item = make_event(self.service)
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5,
            "maneuver_window": WINDOW_A,
        }, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "in_flight")
        command = item["payload"]["maneuver_command"]
        self.assertEqual(command["status"], "in_flight")
        self.assertEqual(command["target_object_id"], "SAT-1")
        # 事件详情：在途指令、占用窗口、回执状态
        self.assertIsNotNone(item["in_flight_command"])
        self.assertEqual(item["occupied_windows"][0]["command_ref"], command["command_ref"])
        self.assertIsNone(command["receipt_status"])

        item = self.service.act(item["id"], "receipt", {"status": "acked"},
                                "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "resolved")
        command = item["payload"]["maneuver_command"]
        self.assertEqual(command["status"], "acked")
        self.assertEqual(command["receipt_status"], "acked")
        # 已确认指令不再占用窗口
        self.assertEqual(item["occupied_windows"], [])
        self.assertIsNone(item["in_flight_command"])
        # 指令链完整落库
        refs = [c["command_ref"] for c in item["commands"]]
        self.assertEqual(refs, [command["command_ref"]])
        audit_types = [event["event_type"] for event in item["audit"]]
        self.assertIn("command_issued", audit_types)
        self.assertIn("receipt_recorded", audit_types)

    def test_no_two_in_flight_commands_same_object(self):
        item1 = make_event(self.service, secondary="DEB-1")
        item2 = make_event(self.service, secondary="DEB-2")
        item1 = self.service.act(item1["id"], "approve", {
            "fuel_cost_m_s": 2.0, "maneuver_window": WINDOW_A,
        }, "coordinator-1", "coordinator", item1["version"])
        with self.assertRaises(Exception) as context:
            self.service.act(item2["id"], "approve", {
                "fuel_cost_m_s": 2.0, "maneuver_window": WINDOW_A,
            }, "coordinator-2", "coordinator", item2["version"])
        self.assertEqual(context.exception.code, "window_overlap")
        # 落选事件退回协调重排，胜出指令仍在途
        loser = self.service.get_item(item2["id"])
        winner = self.service.get_item(item1["id"])
        self.assertEqual(loser["status"], "returned")
        self.assertEqual(winner["status"], "in_flight")
        self.assertEqual(winner["in_flight_command"]["status"], "in_flight")
        # 落选指令是终态、不占窗口；胜出方仍持有同卫星窗口
        self.assertEqual(loser["commands"][0]["status"], "voided")
        self.assertEqual([h["command_ref"] for h in loser["occupied_windows"]],
                         [winner["in_flight_command"]["command_ref"]])
        self.assertIsNone(loser["in_flight_command"])
        item2 = self.service.act(item2["id"], "approve", {
            "fuel_cost_m_s": 2.0, "maneuver_window": WINDOW_B,
        }, "coordinator-2", "coordinator", loser["version"])
        self.assertEqual(item2["status"], "in_flight")
        self.assertEqual(len(item2["commands"]), 2)

    def test_concurrent_overlapping_approvals_only_one_wins(self):
        item1 = make_event(self.service, secondary="DEB-1")
        item2 = make_event(self.service, secondary="DEB-2")
        versions = [item1["version"], item2["version"]]
        ids = [item1["id"], item2["id"]]
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def submit(index):
            barrier.wait()
            try:
                result = self.service.act(ids[index], "approve", {
                    "fuel_cost_m_s": 2.0, "maneuver_window": WINDOW_A,
                }, "coordinator-%d" % (index + 1), "coordinator", versions[index])
                results.append(result["id"])
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(submit, [0, 1]))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "window_overlap")
        # 同物体最终只有一条占用窗口的在途指令
        holder_ids = set()
        for item_id in ids:
            for holder in self.service.get_item(item_id)["occupied_windows"]:
                holder_ids.add(holder["command_ref"])
        self.assertEqual(len(holder_ids), 1)

    def test_failed_receipt_returns_to_coordination_with_reason(self):
        item = make_event(self.service)
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5, "maneuver_window": WINDOW_A,
        }, "coordinator-1", "coordinator", item["version"])
        command_ref = item["payload"]["maneuver_command"]["command_ref"]
        # 失败回执必须带原因
        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "receipt", {"status": "failed"},
                             "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "failure_reason_required")

        item = self.service.act(item["id"], "receipt",
                                {"status": "failed", "reason": "推力器锁定，指令未执行"},
                                "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "failed")
        command = item["payload"]["maneuver_command"]
        self.assertEqual(command["status"], "failed")
        self.assertEqual(command["receipt_status"], "failed")
        self.assertEqual(command["failure_reason"], "推力器锁定，指令未执行")
        # 失败期间窗口仍被保留（不能被其他指令抢走）
        other = make_event(self.service, secondary="DEB-2")
        with self.assertRaises(DomainError) as context:
            self.service.act(other["id"], "approve", {
                "fuel_cost_m_s": 2.0, "maneuver_window": WINDOW_A,
            }, "coordinator-2", "coordinator", other["version"])
        self.assertEqual(context.exception.code, "window_overlap")

        # 重试沿用同一条指令：不同 command_ref 被拒绝
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "retry", {"command_ref": "OTHER"},
                             "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "command_ref_mismatch")

        item = self.service.act(item["id"], "retry", {"command_ref": command_ref},
                                "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "in_flight")
        command = item["payload"]["maneuver_command"]
        self.assertEqual(command["command_ref"], command_ref)
        self.assertEqual(command["attempts"], 2)
        # 仍是同一条指令，没有新增窗口占用
        self.assertEqual(len(item["commands"]), 1)
        self.assertEqual(len(item["occupied_windows"]), 1)

        item = self.service.act(item["id"], "receipt", {"status": "acked"},
                                "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["occupied_windows"], [])

    def test_revision_downgrade_voids_command_and_releases_window(self):
        item = make_event(self.service, distance=120, covariance=100, hours=18)
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5, "maneuver_window": WINDOW_A,
        }, "coordinator-1", "coordinator", item["version"])
        command_ref = item["payload"]["maneuver_command"]["command_ref"]

        # 新观测：距离大幅拉开，等级降到 low，指令不再占优
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-28T05:00:00Z",
            "miss_distance_m": 2000,
            "covariance_m": 100,
            "source": "radar-followup",
        }, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        self.assertEqual(item["status"], "returned")
        command = item["payload"]["maneuver_command"]
        self.assertEqual(command["status"], "voided")
        self.assertEqual(command["command_ref"], command_ref)
        self.assertEqual(item["occupied_windows"], [])

        # 释放出的窗口可以被同物体的其他事件使用
        other = make_event(self.service, secondary="DEB-7", distance=50, hours=2)
        other = self.service.act(other["id"], "approve", {
            "fuel_cost_m_s": 2.0, "maneuver_window": WINDOW_A,
        }, "coordinator-2", "coordinator", other["version"])
        self.assertEqual(other["status"], "in_flight")

    def test_revision_same_level_keeps_command(self):
        item = make_event(self.service, distance=120, hours=18)
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5, "maneuver_window": WINDOW_A,
        }, "coordinator-1", "coordinator", item["version"])
        command_ref = item["payload"]["maneuver_command"]["command_ref"]
        # high -> high（数值变化但等级不变）
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-28T05:00:00Z",
            "miss_distance_m": 10,
            "covariance_m": 100,
            "source": "radar-followup",
        }, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["status"], "in_flight")
        self.assertEqual(item["payload"]["maneuver_command"]["command_ref"], command_ref)
        self.assertEqual(item["payload"]["maneuver_command"]["status"], "in_flight")


if __name__ == "__main__":
    unittest.main()
