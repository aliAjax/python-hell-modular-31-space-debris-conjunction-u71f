import uuid
from datetime import datetime

from .domain import DomainError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "issue_command": {"coordinator"},
    "receipt": {"operator"},
    "retry_command": {"coordinator"},
    "reschedule": {"coordinator"},
    "resolve": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {
    "approve",
    "issue_command",
    "receipt",
    "retry_command",
    "reschedule",
    "resolve",
    "cancel",
}

LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


def assess(payload):
    ratio = float(payload.get("miss_distance_m", 0)) / max(float(payload.get("covariance_m", 1)), 1.0)
    tca_hours = float(payload.get("hours_to_tca", 24))
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    urgency = max(0.0, min(20.0, (24.0 - tca_hours) * 0.8))
    score = round(min(100.0, severity + urgency), 2)
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "distance_to_covariance_ratio": round(ratio, 3)}


def parse_window(window):
    if not isinstance(window, str) or "/" not in window:
        raise DomainError("invalid_window", "规避窗口格式应为 开始时间/结束时间", 400)
    start_text, end_text = [part.strip() for part in window.split("/", 1)]
    if not start_text or not end_text:
        raise DomainError("invalid_window", "规避窗口格式应为 开始时间/结束时间", 400)
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_window", "窗口时间必须是 ISO 时间", 400)
    if start >= end:
        raise DomainError("invalid_window", "窗口开始时间必须早于结束时间", 400)
    return start.isoformat(), end.isoformat()


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _require_number(payload, name, minimum=None):
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError):
        raise DomainError("field_required", "%s 不能为空" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def _require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])
    effects = {}

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = assess(current)
        current["assessment"] = result
        return "assessed", current, {"assessment": result}, effects

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "coordinating", "executing"})
        revision = {
            "observed_at": _require_text(payload, "observed_at"),
            "miss_distance_m": _require_number(payload, "miss_distance_m", 0),
            "covariance_m": _require_number(payload, "covariance_m", 0.001),
            "source": _require_text(payload, "source"),
        }
        if revision["covariance_m"] <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        current.setdefault("revisions", []).append(revision)
        current["miss_distance_m"] = revision["miss_distance_m"]
        current["covariance_m"] = revision["covariance_m"]
        current["assessment"] = assess(current)
        event = {"revision": revision, "assessment": current["assessment"]}
        approved = current.get("approved_maneuver")
        if approved and approved.get("approved_level"):
            old_rank = LEVEL_RANK.get(approved["approved_level"], 0)
            new_rank = LEVEL_RANK.get(current["assessment"]["level"], 0)
            if new_rank < old_rank:
                reason = "新观测后风险等级由 %s 降为 %s，规避指令不再占优" % (
                    approved["approved_level"],
                    current["assessment"]["level"],
                )
                effects["void_in_transit"] = True
                effects["void_reason"] = reason
                event["voided"] = True
                event["void_reason"] = reason
                current.pop("active_command_ref", None)
                return "coordinating", current, event, effects
        return status, current, event, effects

    if action == "record_opinion":
        _need_status(item, {"assessed", "coordinating"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", "")}
        current.setdefault("opinions", []).append(entry)
        if opinion in {"reject", "request_review"}:
            current["conflict"] = True
        return status, current, {"opinion": entry}, effects

    if action == "approve":
        _need_status(item, {"assessed"})
        if current.get("conflict"):
            raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        start, end = parse_window(window)
        level = current.get("assessment", {}).get("level", "low")
        current["approved_maneuver"] = {
            "fuel_cost_m_s": fuel,
            "maneuver_window": window,
            "window_start": start,
            "window_end": end,
            "approved_level": level,
        }
        return "coordinating", current, {"approved_maneuver": current["approved_maneuver"]}, effects

    if action == "issue_command":
        _need_status(item, {"coordinating"})
        maneuver = current.get("approved_maneuver")
        if not maneuver:
            raise DomainError("maneuver_not_approved", "规避方案尚未批准", 409)
        if current.get("active_command_ref"):
            raise DomainError("command_exists", "该事件已有指令：失败请重试，在途请等待回执", 409)
        command_ref = payload.get("command_ref")
        if not isinstance(command_ref, str) or not command_ref.strip():
            command_ref = "CMD-" + uuid.uuid4().hex[:10].upper()
        command = {
            "command_ref": command_ref.strip(),
            "primary_object_id": current["primary_object_id"],
            "secondary_object_id": current["secondary_object_id"],
            "window_start": maneuver["window_start"],
            "window_end": maneuver["window_end"],
            "status": "in_transit",
            "receipt_status": "pending",
            "receipt_reason": None,
            "approved_level": maneuver.get("approved_level"),
        }
        current["active_command_ref"] = command["command_ref"]
        return "executing", current, {"command": command}, {"command": command}

    if action == "receipt":
        _need_status(item, {"executing"})
        command_ref = _require_text(payload, "command_ref")
        result = payload.get("result", "executed")
        if result not in {"executed", "failed"}:
            raise DomainError("invalid_receipt", "回执结果必须是 executed 或 failed")
        active = current.get("active_command_ref")
        if not active or active != command_ref:
            raise DomainError("command_not_found", "没有匹配的在途指令", 404)
        if result == "executed":
            return "executing", current, {"command_ref": command_ref, "result": "executed"}, {
                "receipt": {"command_ref": command_ref, "status": "executed"}
            }
        reason = _require_text(payload, "reason")
        return "coordinating", current, {"command_ref": command_ref, "result": "failed", "reason": reason}, {
            "receipt": {"command_ref": command_ref, "status": "failed", "reason": reason}
        }

    if action == "retry_command":
        _need_status(item, {"coordinating"})
        active = current.get("active_command_ref")
        if not active:
            raise DomainError("no_failed_command", "没有可重试的失败指令", 409)
        return "executing", current, {"command_ref": active, "retried": True}, {
            "retry": {"command_ref": active}
        }

    if action == "reschedule":
        _need_status(item, {"coordinating"})
        if current.get("active_command_ref"):
            raise DomainError("command_in_flight", "存在未结束的指令，不能调整窗口", 409)
        maneuver = current.get("approved_maneuver")
        if not maneuver:
            raise DomainError("maneuver_not_approved", "规避方案尚未批准", 409)
        window = _require_text(payload, "maneuver_window")
        start, end = parse_window(window)
        fuel = payload.get("fuel_cost_m_s")
        if fuel is not None:
            fuel = _require_number(payload, "fuel_cost_m_s", 0)
            budget = float(current.get("fuel_budget_m_s", 0))
            if fuel > budget:
                raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
            maneuver["fuel_cost_m_s"] = fuel
        maneuver["maneuver_window"] = window
        maneuver["window_start"] = start
        maneuver["window_end"] = end
        return "coordinating", current, {"approved_maneuver": maneuver}, {"reschedule": True}

    if action == "resolve":
        _need_status(item, {"executing"})
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        return "resolved", current, {"report_ref": report_ref}, effects

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}, effects

    raise DomainError("unknown_action", "不支持的操作")
