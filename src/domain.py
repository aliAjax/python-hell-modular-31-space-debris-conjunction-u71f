from datetime import datetime, timezone


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def positive_integer(payload, name):
    value = payload.get(name, 0)
    if isinstance(value, bool):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    if value < 0:
        raise DomainError("invalid_integer", "%s 不能为负数" % name)
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    primary = require_text(payload, "primary_object_id")
    secondary = require_text(payload, "secondary_object_id")
    if primary == secondary:
        raise DomainError("same_object", "接近事件的两个物体不能相同")
    tca = parse_timestamp(payload, "tca")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    fuel_budget = number(payload, "fuel_budget_m_s", 0)
    track_age = number(payload, "track_age_hours", 0)
    operators = payload.get("operating_organizations", [])
    if not isinstance(operators, list) or any(not isinstance(item, str) or not item.strip() for item in operators):
        raise DomainError("invalid_operators", "运营方必须是字符串列表")
    stable_key = "%s|%s|%s" % tuple(sorted([primary, secondary]) + [tca])
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "fuel_budget_m_s": fuel_budget,
        "track_age_hours": track_age,
        "operating_organizations": [item.strip() for item in operators],
        "revisions": [],
        "opinions": [],
        "conflict": False,
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "region": region,
        "operator": payload.get("operator"),
    }


def parse_instant(value):
    """解析 ISO 8601 时刻，无时区时按 UTC 处理，返回带时区的 datetime。"""
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_timestamp", "时间必须是 ISO 字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError("invalid_timestamp", "时间必须是 ISO 字符串：%s" % value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def parse_window(value):
    """解析 ISO 8601 时间区间 start/end，返回 (start, end, 归一化字符串)。

    半开区间：[start, end)；允许端点相接（end == start 不算重叠）。
    """
    if not isinstance(value, str) or "/" not in value:
        raise DomainError("invalid_window", "机动窗口格式必须为 start/end 的 ISO 区间")
    start_text, end_text = (part.strip() for part in value.split("/", 1))
    start = parse_instant(start_text)
    end = parse_instant(end_text)
    if end <= start:
        raise DomainError("invalid_window", "机动窗口结束时间必须晚于开始时间")
    normalized = "%s/%s" % (
        start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    )
    return start, end, normalized


def windows_overlap(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a
