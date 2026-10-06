from .domain import DomainError, parse_window

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "receipt": {"operator"},
    "retry": {"operator"},
    "void_command": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "receipt", "retry", "void_command", "cancel"}

LEVEL_RANK = {"high": 3, "medium": 2, "low": 1}

# 已占用窗口的指令状态：在途、回执失败（窗口保留到重试成功或作废）。
WINDOW_HOLD_STATUSES = ("in_flight", "failed")
# 终态：不再占用窗口。
COMMAND_TERMINAL_STATUSES = ("acked", "voided", "superseded")


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


def active_command(payload):
    """返回当前事件仍在占用窗口的指令摘要（在途/失败），没有则 None。"""
    command = payload.get("maneuver_command")
    if command and command.get("status") in WINDOW_HOLD_STATUSES:
        return command
    return None


def prepare_approval(item, payload, actor):
    """协调员批准规避方案：校验燃料/冲突/窗口，返回指令草案。

    窗口与在途指令的互斥判定在 repository 事务内完成（含并发仲裁），
    这里只做纯业务校验和窗口解析。
    """
    current = dict(item["payload"])
    _need_status(item, {"assessed", "returned"})
    if active_command(current):
        raise DomainError("command_in_flight", "该事件已有在途指令，同一时间不能再次下达", 409)
    if current.get("conflict"):
        raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
    fuel = _require_number(payload, "fuel_cost_m_s", 0)
    budget = float(current.get("fuel_budget_m_s", 0))
    if fuel > budget:
        raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
    window_raw = _require_text(payload, "maneuver_window")
    window_start, window_end, window = parse_window(window_raw)
    target = payload.get("target_object_id")
    if target is not None:
        target = str(target).strip()
        if not target:
            raise DomainError("invalid_target", "目标物体不能为空")
    else:
        target = current.get("primary_object_id")
    assessment = current.get("assessment") or assess(current)
    level = assessment.get("level", "low")
    draft = {
        "target_object_id": target,
        "window_start": window_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "window_end": window_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "maneuver_window": window,
        "fuel_cost_m_s": fuel,
        "issued_level": level,
        "issued_by": actor,
    }
    new_payload = dict(current)
    new_payload["approved_maneuver"] = {
        "fuel_cost_m_s": fuel,
        "maneuver_window": window,
        "target_object_id": target,
    }
    return new_payload, draft


def validate_receipt(payload):
    """运营方回执：必须明确成功或失败；失败必须给出原因。"""
    status = _require_text(payload, "status").lower()
    if status not in {"acked", "failed"}:
        raise DomainError("invalid_receipt", "回执状态必须是 acked 或 failed")
    reason = payload.get("reason")
    if reason is not None:
        reason = str(reason).strip()
    if status == "failed" and not reason:
        raise DomainError("failure_reason_required", "回执失败必须填写失败原因")
    return {"status": status, "reason": reason or ""}


def validate_retry(payload):
    """重试沿用同一条指令：command_ref 必须与在途（失败）指令一致。"""
    command_ref = _require_text(payload, "command_ref")
    note = payload.get("note")
    if note is not None:
        note = str(note).strip()
    return command_ref, (note or "")


def void_reason(payload):
    return _require_text(payload, "reason")


def revision_downgrades_command(current):
    """新观测重算等级后，判断当前在途指令是否不再占优。

    规则：等级低于指令下达时的等级（high>medium>low）即作废。
    """
    command = active_command(current)
    if not command:
        return False
    issued = LEVEL_RANK.get(command.get("issued_level", "low"), 1)
    latest = LEVEL_RANK.get(current.get("assessment", {}).get("level", "low"), 1)
    return latest < issued


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = assess(current)
        current["assessment"] = result
        return "assessed", current, {"assessment": result, "actor": actor}, None

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "in_flight", "failed", "returned"})
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
        # 新观测导致在途指令不再占优：作废指令、释放窗口、退回协调重排。
        directive = None
        new_status = status
        if revision_downgrades_command(current):
            command = active_command(current)
            reason = "新观测重算风险等级为 %s，低于指令下达时的 %s，指令不再占优" % (
                current["assessment"]["level"],
                command["issued_level"],
            )
            directive = {"void_current_command": {"command_ref": command["command_ref"], "reason": reason}}
            new_status = "returned"
        return new_status, current, {"revision": revision, "command_voided": directive is not None}, directive

    if action == "record_opinion":
        _need_status(item, {"assessed", "returned", "failed"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", "")}
        current.setdefault("opinions", []).append(entry)
        if opinion in {"reject", "request_review"}:
            current["conflict"] = True
        return status, current, {"opinion": entry}, None

    if action == "approve":
        new_payload, draft = prepare_approval(item, payload, actor)
        # 指令占用窗口与落库由 repository.submit_command 事务完成；
        # 仲裁结果（成立/落选）也在事务内决定。
        return "in_flight", new_payload, {"draft": draft}, {"submit_command": draft}

    if action == "receipt":
        _need_status(item, {"in_flight"})
        command = active_command(current)
        if not command:
            raise DomainError("no_active_command", "没有在途指令可以登记回执", 409)
        receipt = validate_receipt(payload)
        return (
            "resolved" if receipt["status"] == "acked" else "failed",
            current,
            {"command_ref": command["command_ref"], "receipt": receipt},
            {"register_receipt": receipt},
        )

    if action == "retry":
        _need_status(item, {"failed"})
        command = active_command(current)
        if not command:
            raise DomainError("no_active_command", "没有可重试的指令", 409)
        command_ref, note = validate_retry(payload)
        if command_ref != command["command_ref"]:
            raise DomainError(
                "command_ref_mismatch",
                "重试必须沿用同一条指令 %s，不能新建指令占用窗口" % command["command_ref"],
                409,
            )
        return (
            "in_flight",
            current,
            {"command_ref": command_ref, "note": note},
            {"retry_command": {"command_ref": command_ref, "note": note}},
        )

    if action == "void_command":
        _need_status(item, {"in_flight", "failed", "returned"})
        command = active_command(current)
        if not command:
            raise DomainError("no_active_command", "没有在途指令可以作废", 409)
        reason = void_reason(payload)
        return (
            "returned",
            current,
            {"command_ref": command["command_ref"], "reason": reason},
            {"void_command": {"command_ref": command["command_ref"], "reason": reason}},
        )

    if action == "cancel":
        _need_status(item, {"pending", "assessed", "returned"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}, None

    raise DomainError("unknown_action", "不支持的操作")
