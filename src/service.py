from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload, directive = rules.apply_action(item, action, payload, actor, role)

        if directive and "submit_command" in directive:
            # 窗口仲裁在事务内：成功返回事件，落选抛 window_overlap（事件已退回协调）
            self.repository.submit_command(
                item_id, actor, role, new_payload, directive["submit_command"], expected_version
            )
            return self.get_item(item_id)
        if directive and "register_receipt" in directive:
            receipt = directive["register_receipt"]
            self.repository._apply_command_directive(
                item_id, action, actor, role, new_payload, event_payload, expected_version,
                {"kind": "receipt", "receipt": receipt},
            )
            return self.get_item(item_id)
        if directive and "retry_command" in directive:
            retry = directive["retry_command"]
            self.repository._apply_command_directive(
                item_id, action, actor, role, new_payload, event_payload, expected_version,
                {"kind": "retry", "command_ref": retry["command_ref"], "note": retry.get("note", "")},
            )
            return self.get_item(item_id)
        if directive and "void_command" in directive:
            void = directive["void_command"]
            self.repository._apply_command_directive(
                item_id, action, actor, role, new_payload, event_payload, expected_version,
                {"kind": "void", "command_ref": void["command_ref"], "reason": void["reason"]},
            )
            return self.get_item(item_id)
        if directive and "void_current_command" in directive:
            void = directive["void_current_command"]
            # 新观测导致不再占优：与动作落库同一事务作废指令、释放窗口
            self.repository.void_item_command(
                item_id, actor, role, new_payload, event_payload, expected_version, void["reason"]
            )
            return self.get_item(item_id)

        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["commands"] = self.repository.list_commands(item_id)
        item["occupied_windows"] = self.repository.occupied_windows(item_id)
        command = item["payload"].get("maneuver_command")
        item["in_flight_command"] = command if command and command.get("status") in rules.WINDOW_HOLD_STATUSES else None
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
