import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


WINDOW_1 = "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z"
WINDOW_2 = "2026-09-28T08:30:00Z/2026-09-28T09:30:00Z"
WINDOW_3 = "2026-09-28T10:00:00Z/2026-09-28T11:00:00Z"


class CommandChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _assessed_item(self, primary, secondary, tca, distance=120.0, covariance=100.0):
        item = self.service.create_item({
            "primary_object_id": primary,
            "secondary_object_id": secondary,
            "tca": tca,
            "miss_distance_m": distance,
            "covariance_m": covariance,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A"],
        }, "analyst-1", "analyst")
        return self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])

    def _approve(self, item, window, fuel=2.5):
        return self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": fuel,
            "maneuver_window": window,
        }, "coordinator-1", "coordinator", item["version"])

    def _issue(self, item, command_ref):
        return self.service.act(item["id"], "issue_command", {"command_ref": command_ref},
                                "coordinator-1", "coordinator", item["version"])

    def test_overlapping_window_is_rejected_and_loser_returns_to_coordination(self):
        item1 = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item1 = self._approve(item1, WINDOW_1)
        item1 = self._issue(item1, "CMD-1")
        self.assertEqual(item1["commands"][0]["status"], "in_transit")

        item2 = self._assessed_item("SAT-1", "DEB-5", "2026-09-28T12:00:00+00:00")
        item2 = self._approve(item2, WINDOW_2)
        with self.assertRaises(ConflictError) as context:
            self._issue(item2, "CMD-2")
        self.assertEqual(context.exception.code, "window_conflict")

        item2 = self.service.get_item(item2["id"])
        self.assertEqual(item2["status"], "coordinating")
        self.assertEqual(item2["commands"], [])

        item2 = self.service.act(item2["id"], "reschedule", {"maneuver_window": WINDOW_3},
                                 "coordinator-1", "coordinator", item2["version"])
        item2 = self._issue(item2, "CMD-2")
        self.assertEqual(item2["status"], "executing")
        self.assertEqual(item2["commands"][0]["status"], "in_transit")

    def test_adjacent_windows_do_not_conflict(self):
        item1 = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item1 = self._approve(item1, WINDOW_1)
        self._issue(item1, "CMD-1")

        item2 = self._assessed_item("SAT-1", "DEB-5", "2026-09-28T12:00:00+00:00")
        item2 = self._approve(item2, WINDOW_3)
        item2 = self._issue(item2, "CMD-2")
        self.assertEqual(item2["commands"][0]["status"], "in_transit")

    def test_different_objects_can_share_window(self):
        item1 = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item1 = self._approve(item1, WINDOW_1)
        self._issue(item1, "CMD-1")

        item2 = self._assessed_item("SAT-2", "DEB-9", "2026-09-28T12:00:00+00:00")
        item2 = self._approve(item2, WINDOW_1)
        item2 = self._issue(item2, "CMD-2")
        self.assertEqual(item2["commands"][0]["status"], "in_transit")

    def test_receipt_failure_records_reason_and_retry_reuses_same_command(self):
        item = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item = self._approve(item, WINDOW_1)
        item = self._issue(item, "CMD-1")

        item = self.service.act(item["id"], "receipt", {
            "command_ref": "CMD-1", "result": "failed", "reason": "地面站未捕获上行信号",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["commands"][0]["status"], "failed")
        self.assertEqual(item["commands"][0]["receipt_reason"], "地面站未捕获上行信号")

        item = self.service.act(item["id"], "retry_command", {}, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "executing")
        self.assertEqual(len(item["commands"]), 1)
        self.assertEqual(item["commands"][0]["command_ref"], "CMD-1")
        self.assertEqual(item["commands"][0]["status"], "in_transit")

        item = self.service.act(item["id"], "receipt", {
            "command_ref": "CMD-1", "result": "executed",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(item["commands"][0]["receipt_status"], "executed")
        item = self.service.act(item["id"], "resolve", {"report_ref": "RPT-1"}, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "resolved")

    def test_retry_conflicting_window_returns_to_coordination(self):
        item1 = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item1 = self._approve(item1, WINDOW_1)

        item2 = self._assessed_item("SAT-1", "DEB-5", "2026-09-28T12:00:00+00:00")
        item2 = self._approve(item2, WINDOW_2)
        item2 = self._issue(item2, "CMD-2")
        item2 = self.service.act(item2["id"], "receipt", {
            "command_ref": "CMD-2", "result": "failed", "reason": "执行机构故障",
        }, "operator-1", "operator", item2["version"])

        item1 = self.service.act(item1["id"], "reschedule", {"maneuver_window": WINDOW_2},
                                "coordinator-1", "coordinator", item1["version"])
        item1 = self._issue(item1, "CMD-1B")

        with self.assertRaises(ConflictError) as context:
            self.service.act(item2["id"], "retry_command", {}, "coordinator-1", "coordinator", item2["version"])
        self.assertEqual(context.exception.code, "window_conflict")
        item2 = self.service.get_item(item2["id"])
        self.assertEqual(item2["status"], "coordinating")
        self.assertEqual(item2["commands"][0]["status"], "failed")

    def test_revision_lowering_level_voids_command_and_releases_window(self):
        item1 = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item1 = self._approve(item1, WINDOW_1)
        item1 = self._issue(item1, "CMD-1")

        item2 = self._assessed_item("SAT-1", "DEB-5", "2026-09-28T12:00:00+00:00")
        item2 = self._approve(item2, WINDOW_1)
        with self.assertRaises(ConflictError):
            self._issue(item2, "CMD-2")

        item1 = self.service.act(item1["id"], "report_revision", {
            "observed_at": "2026-09-27T10:00:00Z",
            "miss_distance_m": 5000,
            "covariance_m": 100,
            "source": "new-radar-fix",
        }, "analyst-1", "analyst", item1["version"])
        self.assertEqual(item1["status"], "coordinating")
        self.assertEqual(item1["payload"]["assessment"]["level"], "low")
        self.assertEqual(item1["commands"][0]["status"], "voided")

        item2 = self.service.get_item(item2["id"])
        item2 = self._issue(item2, "CMD-2")
        self.assertEqual(item2["commands"][0]["status"], "in_transit")

    def test_revision_raising_level_keeps_command(self):
        item = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00", distance=2000.0)
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-27T10:00:00Z",
            "miss_distance_m": 10,
            "covariance_m": 100,
            "source": "new-radar-fix",
        }, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        self.assertEqual(item["status"], "assessed")

    def test_resolve_requires_executed_receipt(self):
        item = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item = self._approve(item, WINDOW_1)
        item = self._issue(item, "CMD-1")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "resolve", {"report_ref": "RPT-1"},
                             "coordinator-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "receipt_required")

    def test_concurrent_overlapping_submissions_only_one_wins(self):
        item1 = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item1 = self._approve(item1, WINDOW_1)
        item2 = self._assessed_item("SAT-1", "DEB-5", "2026-09-28T12:00:00+00:00")
        item2 = self._approve(item2, WINDOW_2)

        results = []
        barrier = threading.Barrier(2)

        def issue(target, ref):
            barrier.wait()
            try:
                ok = self.service.act(target["id"], "issue_command", {"command_ref": ref},
                                      "coordinator-1", "coordinator", target["version"])
                results.append(("ok", ok["id"]))
            except DomainError as exc:
                results.append(("err", exc.code))

        t1 = threading.Thread(target=issue, args=(item1, "CMD-1"))
        t2 = threading.Thread(target=issue, args=(item2, "CMD-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len([r for r in results if r[0] == "ok"]), 1)
        self.assertEqual(len([r for r in results if r[0] == "err"]), 1)
        self.assertEqual([r for r in results if r[0] == "err"][0][1], "window_conflict")

    def test_item_detail_shows_commands_and_state_shows_occupied_windows(self):
        item = self._assessed_item("SAT-1", "DEB-9", "2026-09-28T12:00:00+00:00")
        item = self._approve(item, WINDOW_1)
        item = self._issue(item, "CMD-1")
        self.assertEqual(item["commands"][0]["window_start"], "2026-09-28T08:00:00+00:00")
        self.assertEqual(item["commands"][0]["window_end"], "2026-09-28T09:00:00+00:00")
        self.assertEqual(item["commands"][0]["receipt_status"], "pending")

        state = self.service.state()
        self.assertEqual(len(state["occupied_windows"]), 1)
        self.assertEqual(state["occupied_windows"][0]["command_ref"], "CMD-1")


if __name__ == "__main__":
    unittest.main()
